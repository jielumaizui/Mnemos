# -*- coding: utf-8 -*-
"""
Memos 双向同步桥接模块

提供：
- 从 Memos 拉取相关内容（保留，作为 L3 查询接口）
- 同步内容到 Memos（将被 SyncEngine 替代）
- SQLite 防重追踪（将被统一 sync_log.db 替代）

⚠️ @deprecated — 部分功能待迁移
  - sync_to_memos() → 新架构使用 SyncEngine.sync_session()
  - 防重数据库 → 统一迁移到 ~/.mnemos/sync_log.db
  - sync_from_memos() → 保留，供 L3 查询使用

设计原则：
- 完全跨平台
- Memos token/api_url 从 get_config() 获取
- SQLite timeout=10
- datetime 用 UTC
"""

from __future__ import annotations

import json
import sqlite3
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Optional

from integrations.styx import MemosClient
from core.kia.ingest_helpers import is_noise_message
from core.config import get_config

logger = logging.getLogger(__name__)


# ==================== _LazyPath ====================

class _LazyPath:
    """Lazy path that resolves get_config() only on access."""
    __slots__ = ('_base', '_segments')

    def __init__(self, base: str = "data_dir", *segments):
        self._base = base
        self._segments = segments

    def __truediv__(self, other):
        return _LazyPath(self._base, *self._segments, other)

    def __rtruediv__(self, other):
        raise NotImplementedError

    def _resolve(self) -> Path:
        config = get_config()
        if self._base == "data_dir":
            result = config.data_dir
        elif self._base == "wiki_dir":
            result = config.wiki_dir
        else:
            result = config.data_dir
        for seg in self._segments:
            result = result / seg
        return result

    def __str__(self):
        return str(self._resolve())

    def __repr__(self):
        return f"LazyPath({self._base}:{'/'.join(self._segments)})"

    def __fspath__(self):
        return str(self._resolve())

    def __getattr__(self, name):
        return getattr(self._resolve(), name)

    def __hash__(self):
        return hash(self._resolve())

    def __eq__(self, other):
        return self._resolve() == other

    def __iter__(self):
        return iter(self._resolve())


# ==================== 路径常量 ====================

DB_PATH = _LazyPath("data_dir", "sync_bridge.db")


def _utcnow() -> datetime:
    """返回带时区的当前 UTC 时间"""
    return datetime.now(timezone.utc)


# ==================== MemosSyncBridge ====================

class MemosSyncBridge:
    """
    Memos 双向同步桥接

    职责：
    - sync_to_memos: 将内容同步到 Memos（含噪声检测和防重）
    - sync_from_memos: 从 Memos 拉取相关内容
    - is_processed / mark_processed: 防重追踪
    """

    def __init__(self, token: str = None, api_url: str = None, agent: str = "bridge"):
        config = get_config()
        self._token = token or config.memos_token
        self._api_url = api_url or config.memos_api_url
        self._agent = agent

        if not self._token:
            raise ValueError("Memos token 未配置（config.yaml 或 MEMOS_TOKEN 环境变量）")

        self.client = MemosClient(
            token=self._token,
            base_url=self._api_url,
            agent=self._agent,
        )

        # 初始化防重数据库
        db_path = Path(DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_db()

    # -------------------- DB --------------------

    def _get_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path), timeout=10)

    def _init_db(self):
        """初始化防重数据库"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_records (
                    session_id TEXT PRIMARY KEY,
                    memos_uid TEXT,
                    content_hash TEXT,
                    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_records_hash
                ON sync_records(content_hash)
            """)
            conn.commit()

    # -------------------- Public API --------------------

    def sync_to_memos(
        self,
        content: str,
        tags: List[str] = None,
        session_id: str = None,
        visibility: str = "PUBLIC",
    ) -> Optional[str]:
        """
        同步内容到 Memos

        Args:
            content: 要同步的内容
            tags: 标签列表
            session_id: 会话 ID（用于防重）
            visibility: 可见性（PUBLIC / PRIVATE）

        Returns:
            Memos UID，如果同步失败或被过滤则返回 None
        """
        # 1. 噪声检测
        if is_noise_message(content):
            logger.debug(f"[SyncBridge] 噪声内容，跳过: {content[:50]}...")
            return None

        # 2. 内容 hash 防重
        content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()[:16]
        if self._is_hash_duplicate(content_hash):
            logger.debug(f"[SyncBridge] 内容已存在（hash={content_hash}），跳过")
            return None

        # 3. session_id 防重
        if session_id and self.is_processed(session_id):
            logger.debug(f"[SyncBridge] session 已处理: {session_id[:16]}...")
            return None

        # 4. 同步到 Memos
        try:
            tags = tags or []
            memos_obj = self.client.save(content, tags, visibility)
            memos_uid = memos_obj.uid if memos_obj else None

            # 5. 标记已处理
            if session_id and memos_uid:
                self.mark_processed(session_id, memos_uid)
            elif content_hash:
                self._record_hash(content_hash, memos_uid)

            logger.info(f"[SyncBridge] 同步成功: {memos_uid}")
            return memos_uid

        except Exception as e:
            logger.error(f"[SyncBridge] 同步失败: {e}")
            return None

    def sync_from_memos(
        self,
        query: str = None,
        tags: List[str] = None,
        limit: int = 20,
    ) -> List[Dict]:
        """
        从 Memos 拉取相关内容

        Args:
            query: 搜索关键词
            tags: 标签过滤
            limit: 最大条数

        Returns:
            匹配的 Memos 记录列表（dict 格式）
        """
        try:
            if tags:
                memories = self.client.list_by_tags(tags, limit=limit)
            elif query:
                memories = self.client.search(query, limit=limit)
            else:
                # 无过滤条件，返回最近记录
                memories = self.client.list_sessions(limit=limit)

            # 转为 dict 列表
            result = []
            for m in memories:
                result.append({
                    "uid": m.uid,
                    "content": m.content,
                    "tags": m.tags,
                    "visibility": m.visibility,
                    "created_at": m.created_at,
                })
            return result

        except Exception as e:
            logger.error(f"[SyncBridge] 拉取失败: {e}")
            return []

    def is_processed(self, session_id: str) -> bool:
        """
        检查是否已处理（防重）

        Args:
            session_id: 会话 ID

        Returns:
            True = 已处理，应跳过
        """
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM sync_records WHERE session_id = ?",
                    (session_id,),
                )
                return cursor.fetchone() is not None
        except Exception as e:
            logger.warning(f"[SyncBridge] 检查防重失败: {e}")
            return False

    def mark_processed(self, session_id: str, memos_uid: str):
        """
        标记已处理（防重）

        Args:
            session_id: 会话 ID
            memos_uid: Memos 记录 UID
        """
        content_hash = None
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT content_hash FROM sync_records WHERE session_id = ?",
                    (session_id,),
                )
                row = cursor.fetchone()
                if row:
                    # 已存在，更新 memos_uid
                    cursor.execute(
                        "UPDATE sync_records SET memos_uid = ?, synced_at = ? WHERE session_id = ?",
                        (memos_uid, _utcnow().isoformat(), session_id),
                    )
                else:
                    cursor.execute(
                        "INSERT INTO sync_records (session_id, memos_uid, synced_at) VALUES (?, ?, ?)",
                        (session_id, memos_uid, _utcnow().isoformat()),
                    )
                conn.commit()
        except Exception as e:
            logger.warning(f"[SyncBridge] 标记已处理失败: {e}")

    # -------------------- Internal --------------------

    def _is_hash_duplicate(self, content_hash: str) -> bool:
        """检查内容 hash 是否已存在"""
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM sync_records WHERE content_hash = ?",
                    (content_hash,),
                )
                return cursor.fetchone() is not None
        except Exception:
            return False

    def _record_hash(self, content_hash: str, memos_uid: str = None):
        """记录内容 hash"""
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR IGNORE INTO sync_records (content_hash, memos_uid, synced_at) VALUES (?, ?, ?)",
                    (content_hash, memos_uid, _utcnow().isoformat()),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[SyncBridge] 记录 hash 失败: {e}")

    # -------------------- Stats --------------------

    def get_stats(self) -> Dict:
        """获取同步统计"""
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM sync_records")
                total = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT COUNT(*) FROM sync_records WHERE memos_uid IS NOT NULL"
                )
                synced = cursor.fetchone()[0]
        except Exception:
            total = 0
            synced = 0

        return {
            "total_records": total,
            "synced_to_memos": synced,
        }
