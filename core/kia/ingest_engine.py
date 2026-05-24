#!/usr/bin/env python3
"""
【已废弃】@deprecated - Ingest Engine

旧的 Clean/Expand 处理引擎已停止维护。功能拆分迁移方向：

L1 原始层：
  - 同步/入库 → core/sync_framework/sync_engine.py (SyncEngine)
  - 防重/噪音过滤 → SyncEngine 统一处理

L2 蒸馏层：
  - 5层防护（self-ref/dedup/pollution/context-recall/PI）
    → core/hephaestus/distill_self_check.py (DistillSelfCheck，零LLM)
  - 质量评分 → core/kia/ingest_helpers.py (score_message_quality)

L3 关联层：
  - 实体/概念提取 → core/connect_worker.py
  - Wiki 页面创建/更新 → 由蒸馏后 HephaestusWorker 触发
  - 索引刷新 → core/kia/ 相关模块

保留此文件作为占位，避免调用方报错。
现有调用方应逐步迁移到 SyncEngine 或对应 L2/L3 模块。
"""

from __future__ import annotations
import os
import sys
import json
import sqlite3
import hashlib
import re
import threading
import queue
import time
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set, Any
from dataclasses import dataclass, asdict
from enum import Enum

# sys.path removed: use project imports directly
from integrations.styx import MemosClient
from core.config import get_config
from core.task_id_parser import TagBuilder

# 精简后的质量与热力追踪
from core.wiki_metrics import (
    WikiMetrics, get_default_metrics,
    compute_evidence_level, compute_knowledge_stage,
    quick_quality_score,
)

# Ingest 纯函数辅助模块（SOT 在 helpers，本类内方法降为 thin wrapper）
from core.kia.ingest_helpers import (
    compute_content_fingerprint,
    is_duplicate_content,
    extract_entities_fallback,
    extract_concepts_fallback,
    extract_entity_description,
    extract_concept_definition,
    detect_wiki_reference_pollution,
    check_wiki_self_reference,
    detect_prompt_injection,
)


# ==================== 配置常量 ====================

class Config:
    """集中配置管理"""
    # Ingest批处理配置
    INGEST_BATCH_SIZE = 10          # 单次最大更新页面数
    INGEST_BATCH_INTERVAL = 10      # 批次间隔（秒）
    INGEST_RETRY_TIMES = 3          # 失败重试次数
    INGEST_RETRY_DELAY = 5          # 重试间隔（秒）

    # Expand触发配置
    EXPAND_SOURCE_THRESHOLD = 3     # Expand触发最小素材数
    EXPAND_HEAT_THRESHOLD = 90      # L3→L4晋级阈值 (90分进入L4才允许Expand)

    # 索引批量更新配置
    INDEX_BATCH_SIZE = 10           # 索引批量刷新批次
    INDEX_FLUSH_INTERVAL = 300      # 索引刷新间隔（秒）

    # 并发控制
    WRITE_LOCK_TIMEOUT = 30         # 写入锁超时（秒）

    # 异常检测配置
    FROZEN_CHECK_WINDOW = 3600      # 异常检测窗口（秒）
    CONFLICT_THRESHOLD = 3          # 冲突次数阈值
    SELF_CHECK_FAILURE_THRESHOLD = 3  # 自检失败阈值
    UPDATE_FREQUENCY_THRESHOLD = 10   # 高频更新阈值（次/小时）


# ==================== 数据模型 ====================

class IngestMode(Enum):
    """Ingest模式"""
    CLEAN = "clean"       # 首次Clean处理
    EXPAND = "expand"     # 后续Expand扩充
    MANUAL = "manual"     # 手动触发


class IngestStatus(Enum):
    """Ingest任务状态"""
    PENDING = "pending"       # 待处理
    PROCESSING = "processing" # 处理中
    COMPLETED = "completed"   # 完成
    FAILED = "failed"         # 失败
    RETRYING = "retrying"     # 重试中


@dataclass
class IngestTask:
    """Ingest任务"""
    task_id: str
    l1_uid: str
    content: str
    source: str
    mode: str  # clean/expand/manual
    tags: List[str]
    status: str = "pending"
    created_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[Dict] = None
    error: Optional[str] = None
    retry_count: int = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


@dataclass
class IngestBatch:
    """Ingest批次"""
    batch_id: str
    tasks: List[IngestTask]
    status: str = "pending"
    created_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


# ==================== Ingest引擎核心 ====================

class IngestEngine:
    """
    Ingest核心引擎（单例模式，防止重复后台线程）

    核心特性：
    1. 串行队列：单线程处理，避免并发冲突
    2. 批量缓冲：内存缓冲，批量刷新索引
    3. Memos记录永久保留
    4. Expand风控：满足条件才允许扩充
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db_path: Optional[str] = None):
        # 防止重复初始化
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        self.db_path = db_path or str(get_config().data_dir / "ingest_engine.db")
        self.state_file = get_config().data_dir / "ingest_state.json"
        self.wiki_root = get_config().wiki_dir
        # P4: Workspace 隔离支持
        self.workspaces = {"claude", "hermes", "shared"}

        self.client = MemosClient(
            token=os.getenv("MEMOS_TOKEN"),
            agent="ingest-engine"
        )
        # 验证 token 已设置
        if not self.client.token:
            raise ValueError("MEMOS_TOKEN 环境变量未设置")

        # 任务队列（串行处理）
        self.task_queue = queue.Queue()
        self.processing = False
        self.worker_thread = None

        # 索引缓冲区
        self.index_buffer = []
        self.last_index_flush = time.time()

        # 写入锁（entity级互斥，防止极端并发）
        self.write_locks = {}  # entity_name -> (threading.Lock(), acquire_time)

        # 精简后的质量与热力追踪
        self.metrics = get_default_metrics()

        self._init_db()
        self._start_worker()

    def _get_wiki_base(self, source: str = "") -> Path:
        """根据 source 获取 Workspace 路径（P4 隔离）

        规则：
        - source 包含 claude → wiki/claude/
        - source 包含 hermes → wiki/hermes/
        - 其他 → wiki/shared/

        如果对应 workspace 目录不存在，自动创建。
        """
        src = (source or "").lower()
        if "claude" in src:
            ws = "claude"
        elif "hermes" in src:
            ws = "hermes"
        else:
            ws = "shared"

        path = self.wiki_root / ws
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _init_db(self):
        """初始化数据库"""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()

            # Ingest任务表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ingest_tasks (
                    task_id TEXT PRIMARY KEY,
                    l1_uid TEXT NOT NULL,
                    content_preview TEXT,
                    source TEXT,
                    mode TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    result TEXT,
                    error TEXT,
                    retry_count INTEGER DEFAULT 0
                )
            """)

            # Ingest批次表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ingest_batches (
                    batch_id TEXT PRIMARY KEY,
                    task_ids TEXT,  -- JSON list
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP
                )
            """)

            # L1归档记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS l1_archive (
                    l1_uid TEXT PRIMARY KEY,
                    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reason TEXT
                )
            """)

            # Entity关联计数表（用于Expand触发判定）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS entity_source_count (
                    entity_name TEXT PRIMARY KEY,
                    source_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    category TEXT
                )
            """)
            # 异常冻结表（全自动无人值守模式）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS entity_frozen (
                    entity_name TEXT PRIMARY KEY,
                    frozen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reason TEXT,  -- 冻结原因
                    conflict_count INTEGER DEFAULT 0,  -- 冲突次数
                    self_check_failures INTEGER DEFAULT 0,  -- 自检失败次数
                    update_frequency INTEGER DEFAULT 0,  -- 更新频率
                    last_conflict_at TIMESTAMP,
                    status TEXT DEFAULT 'frozen'  -- frozen/thawed
                )
            """)

            # 内容去重表 - 防止重复内容进入Wiki
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS content_dedup (
                    content_hash TEXT PRIMARY KEY,
                    first_l1_uid TEXT NOT NULL,  -- 第一个出现的L1记录UID
                    wiki_source_page TEXT,  -- 对应的Wiki Source页面
                    source_type TEXT DEFAULT 'memos',  -- memos/file/chat
                    file_path TEXT,  -- 文件来源路径
                    status TEXT DEFAULT 'processed',  -- processed/duplicate/skipped
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # NOTE: content_dedup 的索引在 _reconcile_columns 之后创建
            # 防止旧表缺少 file_path 列时索引创建失败

            # 来源追踪表 - 记录所有知识来源（文件/聊天记录等）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ingest_source (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,  -- memos/file/chat/unknown
                    source_id TEXT NOT NULL,     -- L1 uid / file path / chat id
                    source_metadata TEXT,        -- JSON 元数据
                    status TEXT DEFAULT 'pending',  -- pending/processed/failed
                    processed_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_source_type_id ON ingest_source(source_type, source_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_source_status ON ingest_source(status)")

            # 文件监控日志表 - 记录文件系统事件
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS file_watch_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    event_type TEXT NOT NULL,  -- created/modified/processed/failed
                    file_hash TEXT,
                    source_id INTEGER,  -- 关联 ingest_source.id
                    error_msg TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fwl_path ON file_watch_log(file_path)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fwl_event ON file_watch_log(event_type)")

            conn.commit()

            # Entity更新日志（用于异常检测）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS entity_update_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_name TEXT NOT NULL,
                    update_type TEXT,  -- content_update/merge/conflict
                    old_content_hash TEXT,
                    new_content_hash TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ========== 声明式 schema 协调（幂等迁移）==========
            # 必须在 CREATE INDEX 之前执行，确保旧表已添加缺失列
            self._reconcile_columns(conn)

            # 索引（在 _reconcile_columns 之后，确保旧表已升级）
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_hash ON content_dedup(content_hash)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_dedup_file ON content_dedup(file_path)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_status ON ingest_tasks(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_l1 ON ingest_tasks(l1_uid)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_frozen_entity ON entity_frozen(entity_name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_update_log_entity ON entity_update_log(entity_name)")

            conn.commit()

    def _reconcile_columns(self, conn: sqlite3.Connection):
        """
        声明式 schema 协调：幂等添加缺失列
        将旧库 schema 自动升级到最新版本，无需手动迁移脚本
        """
        cursor = conn.cursor()

        # entity_source_count 表迁移
        cursor.execute("PRAGMA table_info(entity_source_count)")
        esc_cols = {row[1] for row in cursor.fetchall()}
        for col_name, col_def in [("created_at", "TIMESTAMP"), ("category", "TEXT")]:
            if col_name not in esc_cols:
                cursor.execute(f"ALTER TABLE entity_source_count ADD COLUMN {col_name} {col_def}")
                if col_name == "created_at":
                    cursor.execute("UPDATE entity_source_count SET created_at = last_updated WHERE created_at IS NULL")
                elif col_name == "category":
                    cursor.execute("UPDATE entity_source_count SET category = 'unknown' WHERE category IS NULL")
                print(f"[Ingest] Schema reconciled: entity_source_count.{col_name}")

        # content_dedup 表迁移
        cursor.execute("PRAGMA table_info(content_dedup)")
        dedup_cols = {row[1] for row in cursor.fetchall()}
        for col_name, col_def in [("source_type", "TEXT DEFAULT 'memos'"), ("file_path", "TEXT")]:
            if col_name not in dedup_cols:
                cursor.execute(f"ALTER TABLE content_dedup ADD COLUMN {col_name} {col_def}")
                print(f"[Ingest] Schema reconciled: content_dedup.{col_name}")

        # ingest_tasks 表扩展
        cursor.execute("PRAGMA table_info(ingest_tasks)")
        task_cols = {row[1] for row in cursor.fetchall()}
        for col_name, col_def in [("source_type", "TEXT DEFAULT 'memos'")]:  # memos/file/chat
            if col_name not in task_cols:
                cursor.execute(f"ALTER TABLE ingest_tasks ADD COLUMN {col_name} {col_def}")
                print(f"[Ingest] Schema reconciled: ingest_tasks.{col_name}")

    def _start_worker(self):
        """启动后台工作线程"""
        if self.worker_thread is None or not self.worker_thread.is_alive():
            self.processing = True
            self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self.worker_thread.start()

    def _worker_loop(self):
        """工作线程主循环"""
        while self.processing:
            try:
                task = self.task_queue.get(timeout=1)
                if task:
                    self._process_task(task)
                    self.task_queue.task_done()
            except queue.Empty:
                # 队列为空，检查是否需要刷新索引
                self._check_index_flush()
                continue
            except Exception as e:
                print(f"[Ingest Engine] Worker error: {e}")
                import traceback
                traceback.print_exc()

    def _generate_task_id(self, l1_uid: str) -> str:
        """生成任务ID"""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        hash_input = f"{l1_uid}:{timestamp}"
        short_hash = hashlib.md5(hash_input.encode()).hexdigest()[:8]
        return f"{timestamp}-{short_hash}"

    def submit_task(self, l1_uid: str, content: str, source: str,
                   mode: str = "clean", tags: List[str] = None) -> str:
        """
        提交Ingest任务到队列

        Args:
            l1_uid: L1原始记录UID
            content: 内容
            source: 来源
            mode: clean/expand/manual
            tags: 标签列表

        Returns:
            task_id: 任务ID
        """
        task_id = self._generate_task_id(l1_uid)
        preview = content[:100] + "..." if len(content) > 100 else content

        task = IngestTask(
            task_id=task_id,
            l1_uid=l1_uid,
            content=content,
            source=source,
            mode=mode,
            tags=tags or []
        )

        # 保存到数据库
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ingest_tasks
                (task_id, l1_uid, content_preview, source, mode, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (task_id, l1_uid, preview, source, mode, "pending", task.created_at))
            conn.commit()

        # 加入队列
        self.task_queue.put(task)
        print(f"[Ingest Engine] 任务提交: {task_id} (mode={mode})")

        return task_id

    def submit_batch(self, records: List[Dict], mode: str = "clean") -> str:
        """
        批量提交Ingest任务

        Args:
            records: 记录列表，每项为dict包含l1_uid, content, source
            mode: clean/expand/manual

        Returns:
            batch_id: 批次ID
        """
        batch_id = f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        task_ids = []

        for record in records:
            task_id = self.submit_task(
                l1_uid=record["l1_uid"],
                content=record["content"],
                source=record.get("source", "unknown"),
                mode=mode,
                tags=record.get("tags", [])
            )
            task_ids.append(task_id)

        # 保存批次信息
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ingest_batches (batch_id, task_ids, status, created_at)
                VALUES (?, ?, ?, ?)
            """, (batch_id, json.dumps(task_ids), "pending", datetime.now().isoformat()))
            conn.commit()

        print(f"[Ingest Engine] 批次提交: {batch_id} ({len(records)} 个任务)")
        return batch_id

    def _process_task(self, task: IngestTask):
        """处理单个任务"""
        print(f"[Ingest Engine] 处理任务: {task.task_id}")

        # 更新状态
        self._update_task_status(task.task_id, "processing")
        task.started_at = datetime.now().isoformat()

        try:
            # 根据模式处理
            if task.mode == IngestMode.CLEAN.value:
                result = self._process_clean(task)
            elif task.mode == IngestMode.EXPAND.value:
                result = self._process_expand(task)
            elif task.mode == IngestMode.MANUAL.value:
                result = self._process_manual(task)
            else:
                raise ValueError(f"Unknown mode: {task.mode}")

            # 更新任务状态
            task.status = "completed"
            task.completed_at = datetime.now().isoformat()
            task.result = result
            self._update_task_status(task.task_id, "completed", result=result)

            print(f"[Ingest Engine] 任务完成: {task.task_id}")

        except Exception as e:
            error_msg = str(e)
            print(f"[Ingest Engine] 任务失败: {task.task_id}, error={error_msg}")

            if task.retry_count < Config.INGEST_RETRY_TIMES:
                task.retry_count += 1
                task.status = "retrying"
                self._update_task_status(task.task_id, "retrying", error=error_msg,
                                        retry_count=task.retry_count)
                # 延迟重试
                time.sleep(Config.INGEST_RETRY_DELAY)
                self.task_queue.put(task)
            else:
                task.status = "failed"
                task.error = error_msg
                self._update_task_status(task.task_id, "failed", error=error_msg)

    def _detect_wiki_reference_pollution(self, content: str, tags: List[str]) -> Tuple[bool, float, str]:
        """检测内容是否被 Wiki 引用污染（thin wrapper，实现见 core/ingest_helpers.py）"""
        return detect_wiki_reference_pollution(content, tags)

    def _check_wiki_self_reference(self, content: str) -> Tuple[bool, str, List[str]]:
        """检测内容是否引用了已存在的 Wiki 页面（thin wrapper，实现见 core/ingest_helpers.py）"""
        return check_wiki_self_reference(content)

    def _record_self_reference(self, content_hash: str, l1_uid: str, refs: List[str]) -> None:
        """记录自引用到 content_dedup 表（审计用途，不创建 Wiki 页面）"""
        try:
            with sqlite3.connect(str(self.db_path, timeout=10)) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO content_dedup
                    (content_hash, first_l1_uid, wiki_source_page, status, source_type)
                    VALUES (?, ?, ?, 'duplicate-self-ref', 'self-reference')
                    """,
                    (content_hash, l1_uid, ','.join(refs[:5]))
                )
                conn.commit()
        except Exception as e:
            print(f"  [Ingest] 自引用记录失败（非致命）: {e}")

    def _process_clean(self, task: IngestTask) -> Dict:
        """
        处理Clean模式（首次Ingest）
        创建Source页、Entities、Concepts
        【全自动无人值守】单源禁用结论规则
        【循环污染防护】四层防护机制
        【内容去重】防止重复内容进入Wiki
        """
        # ===== guard:L0-self-ref 防护：自引用标签检测 =====
        if any(tag == 'wiki-ref=do-not-ingest' or 'wiki-ref=do-not-ingest' in tag for tag in (task.tags or [])):
            print(f"  [Ingest] 检测到 wiki-ref=do-not-ingest，跳过实体提取")
            source_page = self._create_source_page(task, guard_tag="guard:L0-self-ref")
            self._mark_l1_processed(task.l1_uid)
            return {
                "source_page": source_page,
                "entities": [],
                "concepts": [],
                "pages_updated": [{"type": "source", "name": task.l1_uid[:16], "status": "created", "guard": "guard:L0-self-ref"}],
                "self_ref_detected": True,
                "guard_layer": "guard:L0-self-ref"
            }

        # ===== arch:dedup (guard:L1) 防护：内容去重检查 =====
        # 使用 ingest_helpers 的指纹算法（清洗后取前100字符MD5）
        content_hash = self._compute_content_fingerprint(task.content)
        is_duplicate, existing_page = self._check_content_duplicate(content_hash)
        if is_duplicate:
            print(f"  [Ingest] ⏭️ 检测到重复内容，跳过处理")
            print(f"  [Ingest]   原始记录: {existing_page}")
            print(f"  [Ingest]   当前记录: {task.l1_uid[:20]}...")
            # 标记为已处理但不创建Wiki页面
            self._mark_l1_processed(task.l1_uid)
            self._record_duplicate(content_hash, task.l1_uid, existing_page)
            return {
                "source_page": None,
                "entities": [],
                "concepts": [],
                "pages_updated": [],
                "duplicate_detected": True,
                "existing_page": existing_page
            }

        # ===== guard:L2-self-ref 防护：Wiki 自引用检测 =====
        # 检测内容中是否包含 [[xxx]] 且 xxx 已在 Wiki 中存在
        is_self_ref, reason, refs = self._check_wiki_self_reference(task.content)
        if is_self_ref:
            print(f"  [Ingest] ⚠️ 检测到 Wiki 自引用 (guard:L2): {reason}")
            print(f"  [Ingest]   引用页面: {', '.join(refs[:3])}")
            # 记录到 content_dedup 表，标记为 duplicate-self-ref
            content_hash = self._compute_content_fingerprint(task.content)
            self._record_self_reference(content_hash, task.l1_uid, refs)
            self._mark_l1_processed(task.l1_uid)
            return {
                "source_page": None,
                "entities": [],
                "concepts": [],
                "pages_updated": [],
                "self_ref_detected": True,
                "self_ref_reason": reason,
                "self_ref_pages": refs,
                "guard_layer": "guard:L2-self-ref"
            }

        # ===== guard:L3 防护：Wiki引用污染检测 =====
        is_polluted, pollution_score, reason = self._detect_wiki_reference_pollution(
            task.content, task.tags
        )
        if is_polluted:
            print(f"  [Ingest] ⚠️ 检测到循环污染风险 (guard:L3): {reason}")
            print(f"  [Ingest] 降低处理优先级，跳过实体提取")
            # 仅创建Source页，不提取实体/概念
            source_page = self._create_source_page(task, guard_tag="guard:L3")
            self._mark_l1_processed(task.l1_uid)
            return {
                "source_page": source_page,
                "entities": [],
                "concepts": [],
                "pages_updated": [{"type": "source", "name": task.l1_uid[:16], "status": "created", "guard": "guard:L3"}],
                "pollution_detected": True,
                "pollution_reason": reason,
                "guard_layer": "guard:L3"
            }

        # ===== guard:L4 防护：上下文回忆污染检测 =====
        # 检测AI是否通过引用Memos历史上下文回答问题
        has_context_recall = any('context:recalled' in tag or tag == 'context:recalled' for tag in task.tags)
        if has_context_recall:
            print(f"  [Ingest] ⚠️ 检测到上下文回忆内容 (guard:L4)")
            print(f"  [Ingest] 防止Memos历史循环进入Wiki，跳过实体提取")
            # 仅创建Source页，不提取实体/概念
            source_page = self._create_source_page(task, guard_tag="guard:L4")
            self._mark_l1_processed(task.l1_uid)
            return {
                "source_page": source_page,
                "entities": [],
                "concepts": [],
                "pages_updated": [{"type": "source", "name": task.l1_uid[:16], "status": "created", "guard": "guard:L4"}],
                "pollution_detected": True,
                "pollution_reason": "Context recall content - prevents circular pollution from Memos history",
                "guard_layer": "guard:L4"
            }

        # ===== guard:PI 防护：Prompt Injection 检测 =====
        is_pi, pi_score, pi_reason, pi_patterns, _ = detect_prompt_injection(task.content)
        if is_pi:
            print(f"  [Ingest] 🚨 检测到 Prompt Injection (guard:PI): {pi_reason}")
            print(f"  [Ingest]   风险分数: {pi_score:.2f}")
            if pi_patterns:
                print(f"  [Ingest]   匹配模式: {', '.join(pi_patterns[:3])}")
            # 仅创建Source页并标记，不提取实体/概念
            source_page = self._create_source_page(task, guard_tag="guard:PI")
            self._mark_l1_processed(task.l1_uid)
            return {
                "source_page": source_page,
                "entities": [],
                "concepts": [],
                "pages_updated": [{"type": "source", "name": task.l1_uid[:16], "status": "created", "guard": "guard:PI"}],
                "prompt_injection_detected": True,
                "pi_score": pi_score,
                "pi_reason": pi_reason,
                "guard_layer": "guard:PI"
            }

        # ===== 质量评估检查 =====
        print(f"  [Ingest] 执行内容质量评估...")
        quality_score = quick_quality_score(task.content)
        if quality_score < 25:
            print(f"  [Ingest] ⚠️ 质量检查未通过: {quality_score:.1f}分")
            source_page = self._create_source_page(task, guard_tag="guard:LQ")
            self._mark_l1_processed(task.l1_uid)
            return {
                "source_page": source_page,
                "entities": [],
                "concepts": [],
                "pages_updated": [{"type": "source", "name": task.l1_uid[:16], "status": "created", "guard": "guard:LQ"}],
                "quality_check_failed": True,
                "quality_score": quality_score,
            }

        print(f"  [Ingest] ✅ 质量检查通过: {quality_score:.1f}分")

        # 提取实体和概念（简化版：使用正则/回退提取）
        entities = self._extract_entities_fallback(task.content)
        concepts = self._extract_concepts_fallback(task.content)
        category = task.tags[0].split('=')[1] if task.tags and '=' in task.tags[0] else "knowledge"

        # 批量写入Wiki
        updated_pages = []

        # 1. 创建Source页
        source_page = self._create_source_page(task)
        updated_pages.append({"type": "source", "name": task.l1_uid[:16], "status": "created"})

        # 2. 创建/更新Entities
        resolved_entities = []
        for entity in entities:
            self._acquire_write_lock(entity)
            try:
                self._update_entity_page(entity, task)
                resolved_entities.append(entity)
                updated_pages.append({"type": "entity", "name": entity, "status": "updated"})
            finally:
                self._release_write_lock(entity)

        # 3. 创建/更新Concepts
        for concept in concepts:
            self._acquire_write_lock(concept)
            try:
                self._update_concept_page(concept, task)
                updated_pages.append({"type": "concept", "name": concept, "status": "updated"})
            finally:
                self._release_write_lock(concept)

        # 4. 标记L1为processed
        self._mark_l1_processed(task.l1_uid)

        # 5. 添加到索引缓冲区
        self.index_buffer.extend(updated_pages)

        return {
            "source_page": source_page,
            "entities": entities,
            "resolved_entities": resolved_entities,
            "concepts": concepts,
            "pages_updated": updated_pages,
            "category": category,
            "quality_score": quality_score,
        }

    def _process_expand(self, task: IngestTask) -> Dict:
        """
        处理Expand模式（后续扩充）- 精简版
        直接复用 Clean 流程，expand 由 curator 接管
        """
        return self._process_clean(task)

    def _process_manual(self, task: IngestTask) -> Dict:
        """处理手动触发模式"""
        # 手动模式可以执行Clean或Expand，根据参数决定
        return self._process_expand(task)

    def _extract_categorized_content(self, content: str, tags: List[str] = None) -> Dict:
        """
        【简化版】实体/概念提取

        直接使用基础提取，不调用 LLM 分类引擎。
        """
        entities = extract_entities_fallback(content)
        concepts = extract_concepts_fallback(content)
        return {
            'content_type': 'unknown',
            'summary': content[:200] + '...' if len(content) > 200 else content,
            'refined_text': content,
            'entities': entities,
            'concepts': concepts,
            'metadata': {'confidence': 0.0, 'features': concepts},
        }

    def _extract_entities_fallback(self, content: str) -> List[str]:
        """实体提取回退方案（thin wrapper，实现见 core/ingest_helpers.py）"""
        return extract_entities_fallback(content)

    def _extract_concepts_fallback(self, content: str) -> List[str]:
        """概念提取回退方案（thin wrapper，实现见 core/ingest_helpers.py）"""
        return extract_concepts_fallback(content)

    def _extract_entities(self, content: str) -> List[str]:
        """提取实体 - 直接调用基础提取"""
        return self._extract_entities_fallback(content)

    def _extract_concepts(self, content: str) -> List[str]:
        """提取概念 - 直接调用基础提取"""
        return self._extract_concepts_fallback(content)

    def _create_source_page(self, task: IngestTask, guard_tag: str = None) -> str:
        """创建Source页并写入文件"""
        date_str = datetime.now().strftime("%Y%m%d")
        filename = f"{date_str}-{task.l1_uid[:8]}.md"
        wiki_base = self._get_wiki_base(task.source)
        file_path = wiki_base / "sources" / filename

        # 构建frontmatter
        frontmatter = {
            "uid": task.l1_uid[:16],
            "type": "source",
            "created_at": task.created_at,
            "processed_at": datetime.now().isoformat(),
            "source": "memos",
            "tags": task.tags
        }
        if guard_tag:
            frontmatter["guard"] = guard_tag

        # 提取实体和概念
        entities = self._extract_entities_fallback(task.content)
        concepts = self._extract_concepts_fallback(task.content)

        content = f"""---
{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False).strip()}
---

# Source: {task.l1_uid[:16]}

**来源**: Memos L1
**时间**: {task.created_at}
**处理**: {datetime.now().isoformat()}

## 原始内容

{task.content[:8000]}{'... (已截断，完整内容见 Memos L1)' if len(task.content) > 8000 else ''}

## 双向链接

"""
        for entity in entities:
            content += f"- [[entities/{entity.lower()}]]\n"

        # 概念链接
        content += "\n## 相关概念\n\n"
        for concept in concepts:
            content += f"- [[concepts/{concept.lower()}]]\n"

        # 写入文件
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding='utf-8')

        # 记录内容哈希（用于去重）
        wiki_page_id = f"sources/{filename}"
        content_hash = self._compute_content_fingerprint(task.content)
        self._record_content_hash(content_hash, task.l1_uid, wiki_page_id)

        # 初始化 wiki_metrics
        # 初始化 wiki_metrics
        try:
            self.metrics.upsert_page(
                path=wiki_page_id,
                title=f"source-{task.l1_uid[:16]}",
                heat_level="warm",
                freshness_days=0,
            )
        except Exception as e:
            print(f"[Ingest] Metrics 初始化失败: {e}")

        return filename

    def _update_entity_page(self, entity: str, task: IngestTask) -> str:
        """
        更新实体页（首次创建或追加）

        写入路径: {wiki_dir}/entities/{entity}.md
        格式: Frontmatter + 关联Source列表 + 汇总描述
        """
        entity_desc = self._extract_entity_description(entity, task.content)
        wiki_base = self._get_wiki_base(task.source)
        entity_path = wiki_base / "entities" / f"{entity.lower()}.md"

        if entity_path.exists():
            self._append_entity_content(entity_path, task, entity_desc, entity)
        else:
            self._create_entity_page(entity_path, entity, task, entity_desc)

        return entity

    def _create_entity_page(self, entity_path: Path, entity: str, task: IngestTask, description: str):
        """创建新的实体页面"""
        initial_source_count = 1
        initial_evidence = compute_evidence_level(initial_source_count)
        initial_stage = compute_knowledge_stage(initial_source_count, "active")
        frontmatter = {
            "name": entity,
            "type": "entity",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "source_count": initial_source_count,
            "sources": [task.l1_uid],
            "status": "active",
            "knowledge_stage": initial_stage,
            "evidence_level": initial_evidence,
        }

        # Source页文件名格式: YYYYMMDD-{uid[:8]}.md
        date_str = datetime.now().strftime("%Y%m%d")
        source_filename = f"{date_str}-{task.l1_uid[:8]}"

        content = f"""---
{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False).strip()}
---

# {entity}

## 描述

{description}

## 关联来源

- [[sources/{source_filename}]] - {task.created_at[:10]}

## 相关实体

"""
        # 提取并链接相关实体
        related = self._extract_entities(task.content)
        for rel in related:
            if rel != entity:
                content += f"- [[entities/{rel.lower()}]]\n"

        entity_path.parent.mkdir(parents=True, exist_ok=True)
        entity_path.write_text(content, encoding='utf-8')

        # 初始化 wiki_metrics
        wiki_page_id = f"entities/{entity.lower()}.md"
        try:
            self.metrics.upsert_page(
                path=wiki_page_id,
                title=entity,
                knowledge_stage=initial_stage,
                evidence_level=initial_evidence,
                source_count=1,
                status="active",
                heat_level="warm",
                freshness_days=0,
            )
            for rel in related:
                if rel != entity:
                    self.metrics.add_relation(wiki_page_id, f"entities/{rel.lower()}.md")
        except Exception as e:
            print(f"[Ingest] Metrics 初始化失败 {wiki_page_id}: {e}")

    def _compute_content_fingerprint(self, content: str) -> str:
        """计算内容指纹（thin wrapper，实现见 core/ingest_helpers.py）"""
        return compute_content_fingerprint(content)

    def _is_duplicate_content(self, existing_body: str, new_description: str, threshold: float = 0.8) -> bool:
        """检测内容是否重复（thin wrapper，实现见 core/ingest_helpers.py）"""
        return is_duplicate_content(existing_body, new_description, threshold)

    def _append_entity_content(self, entity_path: Path, task: IngestTask, description: str, disambiguated_name: str = None):
        """
        追加内容到现有实体页面

        【集成ConflictMerger】Git式三向合并
        - 检测重复内容，避免相同描述被多次添加
        - 智能合并同一实体的多个更新
        - 保留历史有效信息

        Args:
            disambiguated_name: 消歧后的实体名称（如已解析）
        """
        entity_name = disambiguated_name or entity_path.stem
        try:
            content = entity_path.read_text(encoding='utf-8')

            # 解析frontmatter
            import re
            fm_match = re.match(r'^---\n(.*?)\n---\n(.*)$', content, re.DOTALL)
            if fm_match:
                import yaml
                try:
                    frontmatter = yaml.safe_load(fm_match.group(1))
                    body = fm_match.group(2)
                except:
                    frontmatter = {}
                    body = content
            else:
                frontmatter = {}
                body = content

            # ===== 去重检测 =====
            if self._is_duplicate_content(body, description):
                print(f"  [Ingest] 跳过重复内容: {entity_name}")
                return

            # 构建新内容块（简单追加）
            new_content_block = f"""
### 新来源 - {task.created_at[:10]}

{description}
"""

            # 更新frontmatter
            frontmatter["updated_at"] = datetime.now().isoformat()
            frontmatter["source_count"] = frontmatter.get("source_count", 0) + 1
            sources = frontmatter.get("sources", [])
            if task.l1_uid not in sources:
                sources.append(task.l1_uid)
                frontmatter["sources"] = sources

            # 【元数据】重新计算 knowledge_stage + evidence_level
            new_source_count = frontmatter["source_count"]
            current_status = frontmatter.get("status", "active")
            frontmatter["evidence_level"] = compute_evidence_level(new_source_count)
            frontmatter["knowledge_stage"] = compute_knowledge_stage(new_source_count, current_status)

            # 组装最终内容
            new_content = f"""---
{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False).strip()}
---
{body}
{new_content_block}
"""
            entity_path.write_text(new_content, encoding='utf-8')
            print(f"  [Ingest] 已追加内容: {entity_name}")

            # 更新 wiki_metrics
            try:
                related = self._extract_entities(task.content)
                for rel in related:
                    if rel != entity_name:
                        self.metrics.add_relation(
                            f"entities/{entity_name.lower()}.md",
                            f"entities/{rel.lower()}.md"
                        )
                self.metrics.upsert_page(
                    path=f"entities/{entity_name.lower()}.md",
                    knowledge_stage=frontmatter.get("knowledge_stage", "P2"),
                    evidence_level=frontmatter.get("evidence_level", 1),
                    status=frontmatter.get("status", "active"),
                    source_count=new_source_count,
                )
            except Exception as e:
                print(f"[Ingest] Metrics 更新失败 {entity_name}: {e}")

        except Exception as e:
            print(f"[Ingest] 追加实体内容失败 {entity_name}: {e}")

    def _extract_entity_description(self, entity: str, content: str) -> str:
        """从内容中提取实体的描述（thin wrapper，实现见 core/ingest_helpers.py）"""
        return extract_entity_description(entity, content)

    def _update_concept_page(self, concept: str, task: IngestTask):
        """
        更新概念页

        写入路径: {wiki_dir}/concepts/{concept}.md
        """
        wiki_base = self._get_wiki_base(task.source)
        concept_path = wiki_base / "concepts" / f"{concept.lower()}.md"

        # 提取概念定义（从内容中提取）
        concept_def = self._extract_concept_definition(concept, task.content)

        if concept_path.exists():
            self._append_concept_content(concept_path, task, concept_def)
        else:
            self._create_concept_page(concept_path, concept, task, concept_def)

    def _create_concept_page(self, concept_path: Path, concept: str, task: IngestTask, definition: str):
        """创建新的概念页面"""
        initial_source_count = 1
        initial_evidence = compute_evidence_level(initial_source_count)
        initial_stage = compute_knowledge_stage(initial_source_count, "active")
        frontmatter = {
            "name": concept,
            "type": "concept",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "source_count": initial_source_count,
            "sources": [task.l1_uid],
            "status": "active",
            "knowledge_stage": initial_stage,
            "evidence_level": initial_evidence,
        }

        # Source页文件名格式: YYYYMMDD-{uid[:8]}.md
        date_str = datetime.now().strftime("%Y%m%d")
        source_filename = f"{date_str}-{task.l1_uid[:8]}"

        content = f"""---
{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False).strip()}
---

# {concept}

## 定义

{definition}

## 关联来源

- [[sources/{source_filename}]] - {task.created_at[:10]}

## 相关概念

"""
        # 提取并链接相关概念
        concepts = self._extract_concepts(task.content)
        for c in concepts:
            if c != concept:
                content += f"- [[concepts/{c.lower()}]]\n"

        concept_path.parent.mkdir(parents=True, exist_ok=True)
        concept_path.write_text(content, encoding='utf-8')

        # 初始化 wiki_metrics
        wiki_page_id = f"concepts/{concept.lower()}.md"
        try:
            self.metrics.upsert_page(
                path=wiki_page_id,
                title=concept,
                knowledge_stage=initial_stage,
                evidence_level=initial_evidence,
                source_count=1,
                status="active",
                heat_level="warm",
                freshness_days=0,
            )
            for c in concepts:
                if c != concept:
                    self.metrics.add_relation(wiki_page_id, f"concepts/{c.lower()}.md")
        except Exception as e:
            print(f"[Ingest] Metrics 初始化失败 {wiki_page_id}: {e}")

    def _append_concept_content(self, concept_path: Path, task: IngestTask, definition: str):
        """追加内容到现有概念页面"""
        try:
            content = concept_path.read_text(encoding='utf-8')

            import re
            fm_match = re.match(r'^---\n(.*?)\n---\n(.*)$', content, re.DOTALL)
            if fm_match:
                import yaml
                try:
                    frontmatter = yaml.safe_load(fm_match.group(1))
                    body = fm_match.group(2)
                except:
                    frontmatter = {}
                    body = content
            else:
                frontmatter = {}
                body = content

            frontmatter["updated_at"] = datetime.now().isoformat()
            frontmatter["source_count"] = frontmatter.get("source_count", 0) + 1
            sources = frontmatter.get("sources", [])
            if task.l1_uid not in sources:
                sources.append(task.l1_uid)
                frontmatter["sources"] = sources

            # 【元数据】重新计算 knowledge_stage + evidence_level
            new_source_count = frontmatter["source_count"]
            current_status = frontmatter.get("status", "active")
            frontmatter["evidence_level"] = compute_evidence_level(new_source_count)
            frontmatter["knowledge_stage"] = compute_knowledge_stage(new_source_count, current_status)

            new_content = f"""---
{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False).strip()}
---
{body}

### 新来源 - {task.created_at[:10]}

{definition}
"""
            concept_path.write_text(new_content, encoding='utf-8')

            # 更新 wiki_metrics
            try:
                related_concepts = self._extract_concepts(task.content)
                for c in related_concepts:
                    if c != concept_path.stem:
                        self.metrics.add_relation(
                            f"concepts/{concept_path.stem.lower()}.md",
                            f"concepts/{c.lower()}.md"
                        )
                self.metrics.upsert_page(
                    path=f"concepts/{concept_path.stem.lower()}.md",
                    knowledge_stage=frontmatter.get("knowledge_stage", "P2"),
                    evidence_level=frontmatter.get("evidence_level", 1),
                    status=frontmatter.get("status", "active"),
                    source_count=new_source_count,
                )
            except Exception as e:
                print(f"[Ingest] Metrics 更新失败 {concept_path.stem}: {e}")

        except Exception as e:
            print(f"[Ingest] 追加概念内容失败: {e}")

    def _extract_concept_definition(self, concept: str, content: str) -> str:
        """从内容中提取概念的定义（thin wrapper，实现见 core/ingest_helpers.py）"""
        return extract_concept_definition(concept, content)

    def _append_to_entity(self, entity: str, task: IngestTask):
        """
        追加信息到实体页（Expand模式）
        调用_update_entity_page即可（自动处理追加）
        """
        self._update_entity_page(entity, task)

    def _acquire_write_lock(self, entity: str):
        """获取写入锁（线程安全，带超时）"""
        if entity not in self.write_locks:
            self.write_locks[entity] = (threading.Lock(), 0)
        lock, _ = self.write_locks[entity]
        acquired = lock.acquire(timeout=Config.WRITE_LOCK_TIMEOUT)
        if acquired:
            self.write_locks[entity] = (lock, time.time())
        return acquired

    def _release_write_lock(self, entity: str):
        """释放写入锁"""
        if entity in self.write_locks:
            lock, _ = self.write_locks[entity]
            try:
                lock.release()
            except RuntimeError:
                pass  # 锁未被当前线程持有

    def _is_write_locked(self, entity: str) -> bool:
        """检查是否被写入锁锁定（未超时）"""
        if entity not in self.write_locks:
            return False
        lock, acquire_time = self.write_locks[entity]
        # 检查是否超时
        if time.time() - acquire_time > Config.WRITE_LOCK_TIMEOUT:
            # 强制释放过期锁
            try:
                lock.release()
            except RuntimeError:
                pass
            return False
        # 测试锁是否仍被占用
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
            return False
        return True

    # ==================== 异常检测与冻结机制（全自动无人值守）====================

    def _is_entity_frozen(self, entity: str) -> bool:
        """检查实体是否被冻结"""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status FROM entity_frozen
                WHERE entity_name = ? AND status = 'frozen'
            """, (entity,))
            row = cursor.fetchone()
            return row is not None

    def _freeze_entity(self, entity: str, reason: str):
        """
        冻结实体

        触发条件（全自动无人值守模式）：
        1. 短时间内多次出现相互矛盾的信息更新
        2. 连续多次AI自检失败、内容反复不合格
        3. 同一实体高频大量修改，语义波动剧烈
        4. 实体消歧混乱，反复合并拆分
        """
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()

            # 获取当前统计数据
            cursor.execute("""
                SELECT conflict_count, self_check_failures, update_frequency
                FROM entity_frozen WHERE entity_name = ?
            """, (entity,))
            row = cursor.fetchone()

            if row:
                # 更新现有记录
                cursor.execute("""
                    UPDATE entity_frozen
                    SET frozen_at = ?, reason = ?, status = 'frozen'
                    WHERE entity_name = ?
                """, (datetime.now().isoformat(), reason, entity))
            else:
                # 新建冻结记录
                cursor.execute("""
                    INSERT INTO entity_frozen
                    (entity_name, frozen_at, reason, status)
                    VALUES (?, ?, ?, 'frozen')
                """, (entity, datetime.now().isoformat(), reason))

            conn.commit()

        print(f"[Ingest Engine] ⚠️ 实体已冻结: {entity}")
        print(f"  原因: {reason}")

    def _thaw_entity(self, entity: str):
        """解冻实体"""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE entity_frozen
                SET status = 'thawed'
                WHERE entity_name = ?
            """, (entity,))
            conn.commit()
        print(f"[Ingest Engine] ✓ 实体已解冻: {entity}")

    def _enqueue_background_review(self, l1_uid: str, content: str,
                                    entities: List[str], concepts: List[str],
                                    category: str, summary: str) -> None:
        """将新产生的知识条目加入 Background Review 队列

        【H5 Background Review 触发器】
        当 Ingest 处理产生新实体/概念时，记录到队列供后续审查。
        队列文件: ~/.claude/review_queue.jsonl
        """
        try:
            queue_path = Path("~/.claude/review_queue.jsonl").expanduser()
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "enqueued_at": datetime.now().isoformat(),
                "l1_uid": l1_uid,
                "entities": entities,
                "concepts": concepts,
                "category": category,
                "summary": summary,
                "content_preview": content[:500] if content else "",
                "status": "pending",
            }
            with open(queue_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            print(f"  [Ingest] 📋 已加入 Background Review 队列: {l1_uid[:16]}...")
        except Exception as e:
            print(f"  [Ingest] Review 队列写入失败（非致命）: {e}")

    def _log_entity_update(self, entity: str, update_type: str,
                          content: str, old_content: str = None):
        """
        记录实体更新日志（用于异常检测）

        Args:
            entity: 实体名称
            update_type: 更新类型（content_update/merge/conflict）
            content: 新内容
            old_content: 旧内容（可选）
        """
        import hashlib

        new_hash = hashlib.md5(content.encode()).hexdigest()[:16]
        old_hash = hashlib.md5(old_content.encode()).hexdigest()[:16] if old_content else None

        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO entity_update_log
                (entity_name, update_type, old_content_hash, new_content_hash, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (entity, update_type, old_hash, new_hash, datetime.now().isoformat()))
            conn.commit()

        # 更新频率计数并检查异常
        self._check_update_frequency(entity)

    def _check_update_frequency(self, entity: str):
        """检查更新频率是否异常"""
        window_start = (datetime.now() - timedelta(seconds=Config.FROZEN_CHECK_WINDOW)).isoformat()

        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT COUNT(*) FROM entity_update_log
                WHERE entity_name = ? AND timestamp > ?
            """, (entity, window_start))

            count = cursor.fetchone()[0]

        # 更新频率超过阈值则触发冻结
        if count > Config.UPDATE_FREQUENCY_THRESHOLD:
            self._freeze_entity(entity, f"Update frequency too high: {count} updates in {Config.FROZEN_CHECK_WINDOW}s")

    def _record_conflict(self, entity: str):
        """记录冲突"""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()

            # 更新冲突计数
            cursor.execute("""
                INSERT INTO entity_frozen (entity_name, conflict_count, last_conflict_at)
                VALUES (?, 1, ?)
                ON CONFLICT(entity_name) DO UPDATE SET
                    conflict_count = conflict_count + 1,
                    last_conflict_at = ?
            """, (entity, datetime.now().isoformat(), datetime.now().isoformat()))

            # 获取当前冲突次数
            cursor.execute("SELECT conflict_count FROM entity_frozen WHERE entity_name = ?", (entity,))
            row = cursor.fetchone()
            conflict_count = row[0] if row else 0

            conn.commit()

        # 冲突次数超过阈值则冻结
        if conflict_count >= Config.CONFLICT_THRESHOLD:
            self._freeze_entity(entity, f"Too many conflicts: {conflict_count}")

    def _check_recent_conflicts(self, entity: str, hours: int = 24) -> bool:
        """检查最近是否有冲突"""
        window_start = (datetime.now() - timedelta(hours=hours)).isoformat()

        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT COUNT(*) FROM entity_update_log
                WHERE entity_name = ? AND timestamp > ? AND update_type = 'conflict'
            """, (entity, window_start))

            count = cursor.fetchone()[0]

        return count > 0

    def _check_entity_history(self, entity: str) -> bool:
        """
        检查实体历史记录是否优良

        返回: True表示有不良记录
        """
        # 简化实现：检查是否被冻结过
        return self._is_entity_frozen(entity)

    def _get_entity_heat_score(self, entity: str) -> float:
        """获取实体热力评分"""
        try:
            possible_ids = [
                f"entities/{entity.lower()}.md",
                f"concepts/{entity.lower()}.md",
            ]
            for page_id in possible_ids:
                page = self.metrics.get_page(page_id)
                if page:
                    return page.heat_score
        except Exception as e:
            print(f"[Ingest] 获取热力分数失败 {entity}: {e}")
        return 0.0

    def _check_entity_anomalies(self, entity: str) -> Dict:
        """
        全面检查实体异常

        返回异常检测报告
        """
        anomalies = {
            "has_conflicts": self._check_recent_conflicts(entity),
            "is_frozen": self._is_entity_frozen(entity),
            "frozen_info": None
        }

        if anomalies["is_frozen"]:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT frozen_at, reason FROM entity_frozen
                    WHERE entity_name = ?
                """, (entity,))
                row = cursor.fetchone()
                if row:
                    anomalies["frozen_info"] = {
                        "frozen_at": row[0],
                        "reason": row[1]
                    }

        return anomalies

    # ==================== 多源验证机制 v2.0 ====================

    def _check_single_source_constraint(self, entity: str, task: IngestTask) -> Tuple[bool, str, List[Dict]]:
        """
        【多源验证机制 v2.0】单源规则检查

        四级验证等级：
        - Level 1 (单源): 仅 fact
        - Level 2 (2-3源): fact + description
        - Level 3 (4-5源): fact + description + definition + conclusion
        - Level 4 (6+源): 全开

        返回: (是否允许, 原因, 被拦截的表述列表)
        """
        # 简化版：仅检查来源数，不拦截
        source_count = self._get_entity_source_count(entity)
        return True, f"Source count: {source_count}, allow write", []

    def _get_entity_validation_status(self, entity: str) -> Dict:
        """获取实体验证状态（精简版）"""
        count = self._get_entity_source_count(entity)
        return {"entity": entity, "source_count": count}

    def _trigger_auto_release(self, entity: str, new_source_count: int) -> List[Dict]:
        """触发自动释放（精简版：无操作）"""
        return []

    def _increment_entity_source_count(self, entity: str, category: str = "unknown"):
        """增加实体关联Source计数，并记录分类"""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO entity_source_count (entity_name, source_count, category)
                VALUES (?, 1, ?)
                ON CONFLICT(entity_name) DO UPDATE SET
                    source_count = source_count + 1,
                    last_updated = ?,
                    category = COALESCE(NULLIF(?, ''), category)
            """, (entity, category, datetime.now().isoformat(), category))
            conn.commit()

    def _get_entity_source_count(self, entity: str) -> int:
        """获取实体关联Source数量"""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT source_count FROM entity_source_count WHERE entity_name = ?",
                (entity,)
            )
            row = cursor.fetchone()
            return row[0] if row else 0

    def _mark_l1_processed(self, l1_uid: str):
        """标记L1为已处理"""
        try:
            self.client.mark_l1_processed(l1_uid)
        except Exception as e:
            print(f"  [Ingest] 标记L1已处理失败 {l1_uid}: {e}")

    def _check_content_duplicate(self, content_hash: str) -> tuple[bool, Optional[str]]:
        """
        检查内容是否已存在（去重）

        Returns:
            (是否重复, 原始页面ID)
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT wiki_source_page FROM content_dedup WHERE content_hash = ? AND status = 'processed'",
                    (content_hash,)
                )
                row = cursor.fetchone()
                if row:
                    return True, row[0]
        except Exception as e:
            print(f"  [Ingest] 去重检查失败: {e}")
        return False, None

    def _record_duplicate(self, content_hash: str, l1_uid: str, existing_page: str):
        """记录重复内容"""
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO content_dedup (content_hash, first_l1_uid, wiki_source_page, status)
                    VALUES (?, ?, ?, 'duplicate')
                    ON CONFLICT(content_hash) DO NOTHING
                    """,
                    (content_hash, l1_uid, existing_page)
                )
                conn.commit()
        except Exception as e:
            print(f"  [Ingest] 记录重复失败: {e}")

    def _record_content_hash(self, content_hash: str, l1_uid: str, wiki_page: str):
        """记录已处理的内容哈希"""
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO content_dedup (content_hash, first_l1_uid, wiki_source_page, status)
                    VALUES (?, ?, ?, 'processed')
                    ON CONFLICT(content_hash) DO NOTHING
                    """,
                    (content_hash, l1_uid, wiki_page)
                )
                conn.commit()
        except Exception as e:
            print(f"  [Ingest] 记录内容哈希失败: {e}")

    def _update_task_status(self, task_id: str, status: str,
                           result: Dict = None, error: str = None,
                           retry_count: int = None):
        """更新任务状态"""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()

            updates = ["status = ?"]
            params = [status]

            if status == "processing":
                updates.append("started_at = ?")
                params.append(datetime.now().isoformat())

            if status in ("completed", "failed"):
                updates.append("completed_at = ?")
                params.append(datetime.now().isoformat())

            if result is not None:
                updates.append("result = ?")
                params.append(json.dumps(result, ensure_ascii=False))

            if error is not None:
                updates.append("error = ?")
                params.append(error)

            if retry_count is not None:
                updates.append("retry_count = ?")
                params.append(retry_count)

            params.append(task_id)

            cursor.execute(f"""
                UPDATE ingest_tasks
                SET {', '.join(updates)}
                WHERE task_id = ?
            """, params)

            conn.commit()

    def _check_index_flush(self):
        """检查是否需要刷新索引"""
        if not self.index_buffer:
            return

        # 条件1: 缓冲区满
        # 条件2: 超时
        should_flush = (
            len(self.index_buffer) >= Config.INDEX_BATCH_SIZE or
            (time.time() - self.last_index_flush > Config.INDEX_FLUSH_INTERVAL)
        )

        if should_flush:
            self._flush_index()

    def _flush_index(self):
        """
        刷新索引到Wiki
        更新 wiki/docs/TOTAL_INDEX.md
        """
        if not self.index_buffer:
            return

        print(f"[Ingest Engine] 刷新索引: {len(self.index_buffer)} 个页面")

        try:
            # 更新 TOTAL_INDEX.md
            index_path = self.wiki_root / "docs" / "TOTAL_INDEX.md"
            index_path.parent.mkdir(parents=True, exist_ok=True)

            # 读取现有索引
            if index_path.exists():
                content = index_path.read_text(encoding='utf-8')
            else:
                content = "# Wiki 总索引\n\n自动生成的页面索引。\n\n"

            # 添加新条目
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

            new_entries = []
            for item in self.index_buffer:
                item_type = item.get("type", "unknown")
                name = item.get("name", "unknown")
                status = item.get("status", "unknown")

                if item_type == "source":
                    new_entries.append(f"- 📄 [[sources/{name}]] ({status})")
                elif item_type == "entity":
                    new_entries.append(f"- 🏷️ [[entities/{name.lower()}]] ({status})")
                elif item_type == "concept":
                    new_entries.append(f"- 💡 [[concepts/{name.lower()}]] ({status})")

            if new_entries:
                # 添加时间戳分区
                content += f"\n## 更新 {timestamp}\n\n"
                content += "\n".join(new_entries)
                content += "\n"

                # 轮换：超过 2000 行时截断到最近 1000 行
                lines = content.split("\n")
                if len(lines) > 2000:
                    # 保留标题头（前3行）+ 最近 1000 行
                    header = lines[:3]
                    body = lines[-1000:]
                    content = "\n".join(header + ["", "> 已截断旧条目，保留最近 1000 行", ""] + body)
                    print(f"[Ingest Engine] 索引已轮换（{len(lines)} -> {len(content.split(chr(10)))} 行）")

                index_path.write_text(content, encoding='utf-8')
                print(f"[Ingest Engine] 索引已更新: {index_path}")

        except Exception as e:
            print(f"[Ingest Engine] 索引更新失败: {e}")

        # 清空缓冲区
        self.index_buffer = []
        self.last_index_flush = time.time()

    # ==================== 来源追踪接口 ====================

    def record_ingest_source(self, source_type: str, source_id: str,
                             metadata: Dict = None, status: str = "pending") -> int:
        """记录知识来源（文件/聊天记录等）"""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ingest_source (source_type, source_id, source_metadata, status)
                VALUES (?, ?, ?, ?)
            """, (source_type, source_id,
                  json.dumps(metadata, ensure_ascii=False) if metadata else None,
                  status))
            conn.commit()
            return cursor.lastrowid

    def record_file_watch(self, file_path: str, event_type: str,
                          file_hash: str = None, source_id: int = None,
                          error_msg: str = None):
        """记录文件监控事件"""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO file_watch_log (file_path, event_type, file_hash, source_id, error_msg)
                VALUES (?, ?, ?, ?, ?)
            """, (file_path, event_type, file_hash, source_id, error_msg))
            conn.commit()

    # ==================== 查询接口 ====================

    def get_task_status(self, task_id: str) -> Optional[Dict]:
        """获取任务状态"""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT task_id, l1_uid, source, mode, status, created_at, completed_at, error
                FROM ingest_tasks WHERE task_id = ?
            """, (task_id,))
            row = cursor.fetchone()

            if row:
                return {
                    "task_id": row[0],
                    "l1_uid": row[1],
                    "source": row[2],
                    "mode": row[3],
                    "status": row[4],
                    "created_at": row[5],
                    "completed_at": row[6],
                    "error": row[7]
                }
            return None

    def get_stats(self) -> Dict:
        """获取统计信息"""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()

            # 任务统计
            cursor.execute("""
                SELECT status, COUNT(*) FROM ingest_tasks GROUP BY status
            """)
            task_stats = {row[0]: row[1] for row in cursor.fetchall()}

            # Entity统计
            cursor.execute("SELECT COUNT(*), SUM(source_count) FROM entity_source_count")
            entity_row = cursor.fetchone()

        return {
            "tasks": task_stats,
            "queue_size": self.task_queue.qsize(),
            "index_buffer_size": len(self.index_buffer),
            "entities": {
                "count": entity_row[0] or 0,
                "total_sources": entity_row[1] or 0
            }
        }

    def _add_quality_heat_bonus(self, page_id: str) -> float:
        """
        添加质量热力加成（精简版）
        """
        try:
            page = self.metrics.get_page(page_id)
            if page and page.quality_score > 70:
                bonus = 5.0
                print(f"  [Ingest] 质量热力加成: +{bonus}分")
                return bonus
        except Exception as e:
            print(f"  [Ingest] 质量热力加成计算失败: {e}")
        return 0.0

    def stop(self):
        """停止引擎"""
        self.processing = False
        # 刷新剩余索引
        self._flush_index()
        if self.worker_thread:
            self.worker_thread.join(timeout=5)


def main():
    """CLI入口"""
    import argparse
    parser = argparse.ArgumentParser(description="Ingest Engine")
    parser.add_argument("--submit", help="提交单个任务，传入L1 UID")
    parser.add_argument("--batch", help="批量提交，传入JSON文件路径")
    parser.add_argument("--mode", default="clean", help="模式: clean/expand/manual")
    parser.add_argument("--status", help="查询任务状态")
    parser.add_argument("--stats", action="store_true", help="显示统计")

    args = parser.parse_args()

    engine = IngestEngine()

    if args.submit:
        # 简化版：需要传入内容
        print("请使用 --batch 传入完整信息")
    elif args.batch:
        with open(args.batch, 'r', encoding='utf-8') as f:
            records = json.load(f)
        batch_id = engine.submit_batch(records, mode=args.mode)
        print(f"批次已提交: {batch_id}")
    elif args.status:
        status = engine.get_task_status(args.status)
        print(json.dumps(status, indent=2, ensure_ascii=False))
    elif args.stats:
        stats = engine.get_stats()
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
