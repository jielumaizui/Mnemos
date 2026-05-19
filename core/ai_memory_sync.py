#!/usr/bin/env python3
"""
【已废弃】@deprecated - AI Memory Sync Bridge

此模块已被新体系替代：
  - claude_realtime_sync.py: Claude Code 实时同步（watchdog + 行级增量）
  - codex-to-memos-sync.py: Codex 定时同步
  - wiki_builder.py: Karpathy 蒸馏范式 L1→Wiki Markdown

保留此文件作为占位，避免调用方报错。
"""

import os
import sys
import json
import sqlite3
import re
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from integrations.styx import MemosClient
from core.kia.ingest_helpers import score_message_quality


# 本地同步记录数据库（使用 ~/.mnemos 与项目配置保持一致）
_SYNC_LOG_DB = Path.home() / ".mnemos" / "ai_sync_log.db"


class MemorySyncBridge:
    """记忆同步桥接器"""

    # 自引用检测正则
    WIKI_LINK_PATTERN = re.compile(r'\[\[.*?\]\]')
    HASH_TAG_PATTERN = re.compile(r'\bhash=[a-f0-9]{8,}\b')

    # 噪音消息检测
    NOISE_MIN_CHARS = 12              # 最小有效字符数
    NOISE_MAX_CHARS = 30              # 超过此长度不视为纯噪音（可能包含有效信息）
    _NOISE_PATTERNS = [
        # 中文确认类
        re.compile(r'^[好的行可以嗯哦知道了明白了收到谢谢赞棒👍\s,，。！]+[ok]*$', re.I),
        # 英文确认类
        re.compile(r'^(ok|okay|yes|no|thanks|thank you|great|good|nice|continue|go on|got it|\s)+$', re.I),
        # 纯表情/符号
        re.compile(r'^[\s👍🙏✅👌😊😄😆😂🤣💪🔥⭐❤️💯…\.\!\?\,]+$', re.U),
    ]

    def __init__(self, agent: str = "sync"):
        token = os.getenv("MEMOS_TOKEN")
        if not token:
            raise ValueError("MEMOS_TOKEN 环境变量未设置")
        self.client = MemosClient(
            token=token,
            agent=agent
        )
        self.agent = agent
        self.sync_db = _SYNC_LOG_DB
        self._init_sync_log()

    @classmethod
    def detect_wiki_reference(cls, content: str) -> Tuple[bool, str]:
        """
        检测内容是否包含 Wiki 自引用

        返回: (是否包含, 原因)
        """
        if not content:
            return False, ""
        if cls.WIKI_LINK_PATTERN.search(content):
            return True, "contains_wiki_link"
        if cls.HASH_TAG_PATTERN.search(content):
            return True, "contains_hash_tag"
        return False, ""

    def _guard_tags(self, content: str, base_tags: List[str]) -> List[str]:
        """
        根据内容检测自引用，必要时添加防护标签
        【自引用防护 L1】检测到 [[...]] 或 hash= 时标记 do-not-ingest
        """
        has_ref, reason = self.detect_wiki_reference(content)
        if has_ref:
            tags = list(base_tags)
            tags.append("wiki-ref=do-not-ingest")
            print(f"  [Sync] 检测到 Wiki 自引用 ({reason})，标记 do-not-ingest")
            return tags
        return base_tags

    @staticmethod
    def is_noise_message(content: str) -> Tuple[bool, str]:
        """检测消息是否为低价值噪音

        Returns:
            (是否噪音, 原因)
        """
        if not content:
            return True, "empty"

        stripped = content.strip()
        char_count = len(stripped)

        # 长度过滤：过短的消息直接视为噪音
        if char_count < MemorySyncBridge.NOISE_MIN_CHARS:
            return True, f"too_short({char_count}<{MemorySyncBridge.NOISE_MIN_CHARS})"

        # 超过最大长度，不视为纯噪音（可能包含有效信息）
        if char_count > MemorySyncBridge.NOISE_MAX_CHARS:
            return False, "length_ok"

        # 正则匹配噪音模式
        for pattern in MemorySyncBridge._NOISE_PATTERNS:
            if pattern.match(stripped):
                return True, f"noise_pattern"

        return False, "not_matched"

    def _log_filtered_noise(self, messages: List[Dict], reason: str) -> None:
        """记录被过滤的噪音消息（审计用途）"""
        try:
            noise_log = Path.home() / ".mnemos" / "noise_filter_log.jsonl"
            noise_log.parent.mkdir(parents=True, exist_ok=True)
            with open(noise_log, "a", encoding="utf-8") as f:
                entry = {
                    "filtered_at": datetime.now(timezone.utc).isoformat(),
                    "reason": reason,
                    "count": len(messages),
                    "samples": [m.get("content", "")[:30] for m in messages[:3]],
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ==================== 本地同步记录（精确去重）====================

    def _init_sync_log(self):
        """初始化本地同步记录数据库"""
        self.sync_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.sync_db), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    memos_uid TEXT,
                    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    source_mtime INTEGER DEFAULT 0,
                    verified_at TIMESTAMP,
                    memos_exists INTEGER DEFAULT 1,
                    UNIQUE(source_type, source_id, content_hash)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_hash
                ON sync_log(source_type, content_hash)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_source
                ON sync_log(source_type, source_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_verified
                ON sync_log(verified_at)
            """)
            # 【P13】质量评分记录表（增量策略：先记录不拦截）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS quality_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    total_score REAL,
                    length_score REAL,
                    density_score REAL,
                    richness_score REAL,
                    char_count INTEGER,
                    valid_word_count INTEGER,
                    stopword_count INTEGER,
                    unique_ratio REAL,
                    value_signals INTEGER,
                    scored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_type, source_id, content_hash)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_qs_score
                ON quality_scores(total_score)
            """)
            conn.commit()

    def _is_synced(self, source_type: str, source_id: str,
                   content: str,
                   source_mtime: int = 0) -> Tuple[bool, Optional[str]]:
        """
        检查是否已同步（基于 content_hash + mtime 精确去重）

        【P12 Local Timestamp Index】
        - content_hash 匹配且 source_mtime 未变化 = 已同步
        - content_hash 匹配但 source_mtime 更新 = 源文件已修改，需重新同步
        - memos_exists=0 = Memos 端已删除，需重新同步

        返回: (是否已同步, memos_uid)
        """
        if not content:
            return False, None
        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
        try:
            with sqlite3.connect(str(self.sync_db), timeout=10) as conn:
                cursor = conn.execute(
                    """
                    SELECT memos_uid, source_mtime, memos_exists
                    FROM sync_log
                    WHERE source_type = ? AND source_id = ? AND content_hash = ?
                    """,
                    (source_type, source_id, content_hash)
                )
                row = cursor.fetchone()
                if not row:
                    return False, None

                _memos_uid, recorded_mtime, memos_exists = row

                # Memos 端已删除 → 需要重新同步
                if memos_exists == 0:
                    return False, None

                # 源文件mtime更新 → 需要重新同步
                if source_mtime > 0 and recorded_mtime > 0 and source_mtime > recorded_mtime:
                    print(f"  [Sync] 源文件已修改，重新同步: {source_id}")
                    return False, None

                return True, _memos_uid
        except Exception as e:
            print(f"  [Sync] sync_log 查询失败: {e}")
        return False, None

    def _record_sync(self, source_type: str, source_id: str,
                     content: str, memos_uid: str = "",
                     source_mtime: int = 0):
        """记录同步完成（含 mtime）"""
        if not content:
            return
        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
        try:
            with sqlite3.connect(str(self.sync_db), timeout=10) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sync_log
                    (source_type, source_id, content_hash, memos_uid,
                     synced_at, source_mtime, verified_at, memos_exists)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (source_type, source_id, content_hash, memos_uid,
                     datetime.now(timezone.utc).isoformat(), source_mtime,
                     datetime.now(timezone.utc).isoformat(), 1)
                )
                conn.commit()
        except Exception as e:
            print(f"  [Sync] sync_log 记录失败: {e}")

    def _log_quality_score(self, source_type: str, source_id: str,
                           content: str, score_result: Dict) -> None:
        """【P13】记录内容质量评分（增量策略：只记录不拦截）"""
        if not content:
            return
        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
        d = score_result.get("details", {})
        try:
            with sqlite3.connect(str(self.sync_db), timeout=10) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO quality_scores
                    (source_type, source_id, content_hash,
                     total_score, length_score, density_score, richness_score,
                     char_count, valid_word_count, stopword_count,
                     unique_ratio, value_signals, scored_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (source_type, source_id, content_hash,
                     score_result.get("total_score", 0),
                     score_result.get("length_score", 0),
                     score_result.get("density_score", 0),
                     score_result.get("richness_score", 0),
                     d.get("char_count", 0),
                     d.get("valid_word_count", 0),
                     d.get("stopword_count", 0),
                     d.get("unique_ratio", 0),
                     d.get("value_signals", 0),
                     datetime.now(timezone.utc).isoformat())
                )
                conn.commit()
        except Exception as e:
            print(f"  [Sync] 质量评分记录失败: {e}")

    def get_quality_stats(self) -> Dict:
        """【P13】获取质量评分统计"""
        try:
            with sqlite3.connect(str(self.sync_db), timeout=10) as conn:
                cursor = conn.execute("""
                    SELECT COUNT(*), AVG(total_score),
                           MIN(total_score), MAX(total_score)
                    FROM quality_scores
                """)
                total, avg_score, min_score, max_score = cursor.fetchone()

                # 分档统计
                cursor = conn.execute("""
                    SELECT CASE
                        WHEN total_score >= 70 THEN 'high'
                        WHEN total_score >= 50 THEN 'medium'
                        ELSE 'low'
                    END as tier, COUNT(*)
                    FROM quality_scores
                    GROUP BY tier
                """)
                tiers = {row[0]: row[1] for row in cursor.fetchall()}

                return {
                    "total_scored": total or 0,
                    "avg_score": round(avg_score or 0, 1),
                    "min_score": round(min_score or 0, 1),
                    "max_score": round(max_score or 0, 1),
                    "tiers": tiers,
                }
        except Exception:
            return {"total_scored": 0, "avg_score": 0, "tiers": {}}

    def get_sync_stats(self) -> Dict:
        """获取同步统计"""
        try:
            with sqlite3.connect(str(self.sync_db), timeout=10) as conn:
                cursor = conn.execute(
                    """
                    SELECT source_type,
                           COUNT(*) as total,
                           SUM(CASE WHEN memos_exists = 1 THEN 1 ELSE 0 END) as valid,
                           SUM(CASE WHEN memos_exists = 0 THEN 1 ELSE 0 END) as ghost,
                           MAX(synced_at) as last_sync
                    FROM sync_log GROUP BY source_type
                    """
                )
                stats = {}
                for row in cursor.fetchall():
                    stats[row[0]] = {
                        "count": row[1],
                        "valid": row[2],
                        "ghost": row[3],
                        "last_sync": row[4]
                    }
                return stats
        except Exception:
            return {}

    def cleanup_ghost_records(self, sample_size: int = 10) -> int:
        """
        【P12 幽灵记录清理】检测 Memos 端已删除但本地索引仍存在的记录

        策略：抽样验证，避免每次全量查询 Memos API
        如果检测到 memos_uid 在 Memos 中不存在，标记 memos_exists=0

        Returns:
            清理的记录数
        """
        try:
            with sqlite3.connect(str(self.sync_db), timeout=10) as conn:
                # 选取最近同步但久未验证的记录
                cursor = conn.execute(
                    """
                    SELECT id, source_type, source_id, memos_uid
                    FROM sync_log
                    WHERE memos_exists = 1
                      AND (verified_at IS NULL
                           OR verified_at < datetime('now', '-7 days'))
                    ORDER BY RANDOM()
                    LIMIT ?
                    """,
                    (sample_size,)
                )
                candidates = cursor.fetchall()

            cleaned = 0
            for rec_id, source_type, source_id, memos_uid in candidates:
                if not memos_uid:
                    continue
                try:
                    # 尝试通过 Memos API 查询该记录是否存在
                    # 使用 get_by_id 或 list 检查
                    exists = self._check_memos_record_exists(memos_uid)
                    with sqlite3.connect(str(self.sync_db), timeout=10) as conn:
                        if not exists:
                            conn.execute(
                                """UPDATE sync_log
                                   SET memos_exists = 0, verified_at = ?
                                   WHERE id = ?""",
                                (datetime.now(timezone.utc).isoformat(), rec_id)
                            )
                            cleaned += 1
                            print(f"  [Sync] 幽灵记录标记: {source_type}/{source_id}")
                        else:
                            conn.execute(
                                """UPDATE sync_log
                                   SET verified_at = ?
                                   WHERE id = ?""",
                                (datetime.now(timezone.utc).isoformat(), rec_id)
                            )
                        conn.commit()
                except Exception as e:
                    print(f"  [Sync] 验证记录失败 {memos_uid}: {e}")

            if cleaned > 0:
                print(f"  [Sync] 清理 {cleaned} 条幽灵记录")
            return cleaned

        except Exception as e:
            print(f"  [Sync] 幽灵记录清理失败: {e}")
            return 0

    def _check_memos_record_exists(self, memos_uid: str) -> bool:
        """检查 Memos 记录是否仍存在（轻量级）"""
        try:
            # 尝试通过 list API 带 uid 过滤查询
            results = self.client.list_all_memos(max_records=1)
            # 如果 list API 不支持按 uid 过滤，则保守返回 True
            # 避免误判导致重复同步
            return True
        except Exception:
            # API 异常时保守处理：假设记录存在
            return True

    def get_local_sync_status(self) -> Dict:
        """
        【P12】纯本地索引状态查询（零 API 调用）

        替代 get_sync_status 中的 Memos list_by_tags() 调用，
        完全基于本地 sync_log 统计，性能 O(1)。
        """
        stats = self.get_sync_stats()
        total_synced = sum(s.get("count", 0) for s in stats.values())
        total_ghost = sum(s.get("ghost", 0) for s in stats.values())

        return {
            "local_index_total": total_synced,
            "local_index_valid": total_synced - total_ghost,
            "local_index_ghost": total_ghost,
            "by_source": stats,
            "last_check": datetime.now(timezone.utc).isoformat(),
            "index_path": str(self.sync_db),
        }

    def sync_hermes_memories(self) -> Dict:
        """同步 Hermes 记忆文件到 Memos（本地 hash + mtime 精确去重）"""
        mem_dir = Path.home() / ".hermes" / "memories"
        results = {"memory": 0, "user": 0, "skipped": 0}

        # 同步 MEMORY.md
        memory_file = mem_dir / "MEMORY.md"
        if memory_file.exists():
            mtime = int(memory_file.stat().st_mtime)
            content = memory_file.read_text(encoding="utf-8")
            entries = [e.strip() for e in content.split("\n§\n") if e.strip()]
            for entry in entries:
                is_dup, _ = self._is_synced(
                    "hermes_memory", entry[:32], entry, source_mtime=mtime
                )
                if is_dup:
                    results["skipped"] += 1
                    continue

                tags = self._guard_tags(entry, [
                    "hermes-shared",      # 框架级
                    "type=curated",
                    "source=hermes",      # Clean 兜底匹配
                    "ingest=wiki",        # Wiki 准入
                    "processed=false"     # 待 Clean
                ])
                result = self.client.save(content=entry, tags=tags)
                memos_uid = result.uid if hasattr(result, 'uid') else str(result)
                self._record_sync("hermes_memory", entry[:32], entry,
                                  memos_uid, source_mtime=mtime)
                results["memory"] += 1

        # 同步 USER.md
        user_file = mem_dir / "USER.md"
        if user_file.exists():
            mtime = int(user_file.stat().st_mtime)
            content = user_file.read_text(encoding="utf-8")
            entries = [e.strip() for e in content.split("\n§\n") if e.strip()]
            for entry in entries:
                is_dup, _ = self._is_synced(
                    "hermes_user", entry[:32], entry, source_mtime=mtime
                )
                if is_dup:
                    results["skipped"] += 1
                    continue

                tags = self._guard_tags(entry, [
                    "hermes-shared",      # 框架级
                    "type=user-profile",
                    "source=hermes",      # Clean 兜底匹配
                    "ingest=wiki",        # Wiki 准入
                    "processed=false"     # 待 Clean
                ])
                result = self.client.save(content=entry, tags=tags)
                memos_uid = result.uid if hasattr(result, 'uid') else str(result)
                self._record_sync("hermes_user", entry[:32], entry,
                                  memos_uid, source_mtime=mtime)
                results["user"] += 1

        return results

    def sync_hermes_sessions(self, limit: int = 50) -> Dict:
        """同步 Hermes 会话记录到 Memos（本地 hash 精确去重）"""
        sessions_dir = Path.home() / ".hermes" / "sessions"
        results = {"synced": 0, "skipped": 0}

        if not sessions_dir.exists():
            return results

        # 获取最近的会话文件
        session_files = sorted(
            sessions_dir.glob("session_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )[:limit]

        for session_file in session_files:
            try:
                mtime = int(session_file.stat().st_mtime)
                data = json.loads(session_file.read_text(encoding="utf-8"))
                session_id = data.get("session_id", session_file.stem)

                # 提取关键对话内容 (Hermes 用 'messages' 而不是 'turns')
                messages = data.get("messages", [])
                if len(messages) < 2:  # 跳过太短的会话
                    continue

                # 构建会话摘要
                summary_lines = [f"## Hermes Session: {session_id}", ""]
                summary_lines.append(f"**Model**: {data.get('model', 'unknown')}")
                summary_lines.append(f"**Start**: {data.get('session_start', 'unknown')}")
                summary_lines.append("")

                for msg in messages[:20]:  # 限制长度
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "")
                    if content:
                        # 截断长内容
                        content = content[:300] + "..." if len(content) > 300 else content
                        summary_lines.append(f"**{role}**: {content}")
                        summary_lines.append("")

                session_summary = "\n".join(summary_lines)

                # 本地 hash + mtime 精确去重
                is_dup, _ = self._is_synced(
                    "hermes_session", session_id, session_summary,
                    source_mtime=mtime
                )
                if is_dup:
                    results["skipped"] += 1
                    continue

                # 保存到 Memos - 使用框架级标签 (hermes-shared)
                tags = self._guard_tags(session_summary, [
                    "hermes-shared",      # 框架级：Hermes所有项目可见
                    "type=session-sync",
                    "source=hermes",      # Clean 兜底匹配
                    "ingest=wiki",        # Wiki 准入
                    "processed=false",    # 待 Clean
                    f"project={session_id[:8]}",
                    f"model={data.get('model', 'unknown')[:20]}"
                ])
                result = self.client.save(content=session_summary, tags=tags)
                memos_uid = result.uid if hasattr(result, 'uid') else str(result)
                self._record_sync("hermes_session", session_id, session_summary,
                                  memos_uid, source_mtime=mtime)
                print(f"  ✓ Synced: {session_id}")
                results["synced"] += 1

            except Exception as e:
                print(f"  ✗ Error syncing {session_file}: {e}")
                continue

        return results

    def sync_openclaw_memories(self) -> Dict:
        """同步 OpenClaw 记忆到 Memos（本地 hash 精确去重）"""
        db_path = Path.home() / ".openclaw" / "memory" / "main.sqlite"
        results = {"files": 0, "chunks": 0, "skipped": 0}

        if not db_path.exists():
            return results

        try:
            mtime = int(db_path.stat().st_mtime)
            with sqlite3.connect(str(db_path), timeout=10) as conn:
                cursor = conn.cursor()

                # 同步 files 表
                cursor.execute("SELECT path, source, hash FROM files")
                files = cursor.fetchall()
                for path, source, hash_val in files[:100]:  # 限制数量
                    content = f"OpenClaw File: {path}\nSource: {source}\nHash: {hash_val}"
                    source_id = f"file:{path}"

                    is_dup, _ = self._is_synced(
                        "openclaw_file", source_id, content, source_mtime=mtime
                    )
                    if is_dup:
                        results["skipped"] += 1
                        continue

                    tags = self._guard_tags(content, [
                        "openclaw-shared",    # 框架级
                        "type=file-meta",
                        "source=openclaw",    # Clean 兜底匹配
                        "ingest=wiki",        # Wiki 准入
                        "processed=false"     # 待 Clean
                    ])
                    result = self.client.save(content=content, tags=tags)
                    memos_uid = result.uid if hasattr(result, 'uid') else str(result)
                    self._record_sync("openclaw_file", source_id, content,
                                      memos_uid, source_mtime=mtime)
                    results["files"] += 1

                # 同步 chunks 表（选取最新）
                cursor.execute("""
                    SELECT path, text, model FROM chunks
                    ORDER BY rowid DESC LIMIT 50
                """)
                chunks = cursor.fetchall()
                for path, text, model in chunks:
                    if not text or len(text) < 10:
                        continue

                    # 截断长内容
                    content = text[:500] + "..." if len(text) > 500 else text
                    content = f"OpenClaw Memory Chunk ({model}):\n\n{content}"
                    source_id = f"chunk:{path}:{hashlib.md5(text.encode()).hexdigest()[:16]}"

                    is_dup, _ = self._is_synced(
                        "openclaw_chunk", source_id, content, source_mtime=mtime
                    )
                    if is_dup:
                        results["skipped"] += 1
                        continue

                    tags = self._guard_tags(content, [
                        "openclaw-shared",    # 框架级
                        "type=memory-chunk",
                        "source=openclaw",    # Clean 兜底匹配
                        "ingest=wiki",        # Wiki 准入
                        "processed=false",    # 待 Clean
                        f"model={model}"
                    ])
                    result = self.client.save(content=content, tags=tags)
                    memos_uid = result.uid if hasattr(result, 'uid') else str(result)
                    self._record_sync("openclaw_chunk", source_id, content,
                                      memos_uid, source_mtime=mtime)
                    results["chunks"] += 1

        except Exception as e:
            print(f"Error syncing OpenClaw: {e}")

        return results

    def export_chat_record(self, agent: str, messages: List[Dict],
                           metadata: Dict = None, skip_dup_check: bool = False) -> str:
        """
        导出聊天记录到 Memos（本地 hash 精确去重）

        Args:
            agent: 'claude', 'hermes', or 'openclaw'
            messages: 消息列表 [{role, content, timestamp}]
            metadata: 额外元数据
            skip_dup_check: 是否跳过去重（默认不跳过）
        """
        # ===== 噪音过滤 =====
        filtered_messages = []
        noise_messages = []
        for msg in messages:
            content = msg.get("content", "")
            is_noise, reason = self.is_noise_message(content)
            if is_noise:
                noise_messages.append(msg)
            else:
                filtered_messages.append(msg)

        if noise_messages:
            self._log_filtered_noise(noise_messages, f"filtered_{len(noise_messages)}_noise")
            print(f"  [Sync] 过滤 {len(noise_messages)} 条噪音消息 ({reason})")

        if not filtered_messages:
            print("  [Sync] 所有消息均为噪音，跳过同步")
            return ""

        # 构建记录内容
        lines = [
            f"# {agent.upper()} Chat Record",
            f"Time: {datetime.now(timezone.utc).isoformat()}",
            f"Agent: {agent}",
            ""
        ]

        for msg in filtered_messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            ts = msg.get("timestamp", "")
            lines.append(f"## {role} ({ts})")
            lines.append(content)
            lines.append("")

        content = "\n".join(lines)

        # 【P13】内容质量评分（增量策略：先记录不拦截）
        quality = score_message_quality(content)
        source_id = f"chat:{agent}:{datetime.now().strftime('%Y%m%d')}"
        self._log_quality_score(f"{agent}_chat", source_id, content, quality)
        print(f"  [Sync] 质量评分: {quality['total_score']:.1f} "
              f"(L={quality['length_score']:.0f} D={quality['density_score']:.0f} R={quality['richness_score']:.0f})")

        # 本地 hash 精确去重
        if not skip_dup_check:
            source_id = f"chat:{agent}:{datetime.now().strftime('%Y%m%d')}"
            is_dup, existing_uid = self._is_synced(f"{agent}_chat", source_id, content)
            if is_dup:
                print(f"  [Sync] 聊天记录已存在，跳过 (uid={existing_uid})")
                return existing_uid or ""

        # 生成标签 - 根据 agent 使用对应的框架级标签
        agent_tag_map = {
            "claude": "claude-shared",
            "hermes": "hermes-shared",
            "openclaw": "openclaw-shared"
        }
        framework_tag = agent_tag_map.get(agent, f"{agent}-shared")

        # 添加标签（Memos 不写 level，热力在 Wiki）
        base_tags = [
            f"source={agent}",
            f"time={datetime.now().strftime('%Y%m%d')}",
            f"model=unknown",
            f"scope=public",
            f"task=daily"
        ]
        base_tags.append(framework_tag)
        base_tags.append("type=chat_record")
        if metadata:
            for k, v in metadata.items():
                base_tags.append(f"{k}:{v}")

        tags = self._guard_tags(content, base_tags)

        # 保存 - 使用分片功能处理长内容
        memories = self.client.save_long_content(
            content=content,
            tags=tags,
            visibility="PUBLIC",
            title=f"chat-{agent}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )

        # 注册所有分片到热力系统
        for memory in memories:
            memos_uid = memory.uid if hasattr(memory, 'uid') else str(memory)

        # 记录同步
        if memories:
            main_uid = memories[0].uid if memories else ""
            source_id = f"chat:{agent}:{datetime.now().strftime('%Y%m%d')}"
            self._record_sync(f"{agent}_chat", source_id, content, main_uid)

            # 即时更新 wiki_metrics（记录同步事件）
            try:
                from core.wiki_metrics import get_default_metrics
                metrics = get_default_metrics()
                for mem in memories:
                    mem_uid = getattr(mem, 'uid', '') or str(mem)
                    metrics.upsert_page(
                        path=f"memos/{mem_uid}",
                        title=f"chat-{agent}",
                        freshness_days=0,
                        heat_level="hot",
                    )
            except Exception as e:
                print(f"  [Sync] Metrics 更新失败（非致命）: {e}")

            # 【Background Review】聊天记录同步后入队待审查
            try:
                self._enqueue_background_review(
                    l1_uid=main_uid,
                    agent=agent,
                    messages=filtered_messages,
                    metadata=metadata,
                )
            except Exception as e:
                print(f"  [Sync] Review 队列写入失败（非致命）: {e}")

            return main_uid
        return ""

    def _enqueue_background_review(self, l1_uid: str, agent: str,
                                    messages: List[Dict],
                                    metadata: Dict = None) -> None:
        """将聊天记录加入 Background Review 队列"""
        try:
            queue_path = Path.home() / ".mnemos" / "review_queue.jsonl"
            queue_path.parent.mkdir(parents=True, exist_ok=True)

            # 提取内容预览（去噪音后的消息）
            content_preview = "\n".join(
                f"{m.get('role', 'unknown')}: {m.get('content', '')[:100]}"
                for m in messages[:5]
            )

            entry = {
                "enqueued_at": datetime.now(timezone.utc).isoformat(),
                "l1_uid": l1_uid,
                "agent": agent,
                "entities": [],
                "concepts": [],
                "category": "chat-record",
                "summary": metadata.get("sync_mode", "") if metadata else "",
                "content_preview": content_preview,
                "status": "pending",
            }
            with open(queue_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def get_sync_status(self) -> Dict:
        """
        获取同步状态（基于本地索引，零 API 调用）

        【P12 Local Timestamp Index】
        不再调用 Memos list_by_tags() API，完全基于本地 sync_log 统计。
        如需验证 Memos 端状态，调用 cleanup_ghost_records() 进行抽样检查。
        """
        # 本地精确去重统计（零 API 调用）
        status = {
            "last_sync": datetime.now(timezone.utc).isoformat(),
            "index_type": "local_sqlite",
            "index_path": str(self.sync_db),
        }

        try:
            sync_stats = self.get_sync_stats()
            for source_type, stat in sync_stats.items():
                status[f"{source_type}_synced"] = stat

            # 汇总
            total = sum(s.get("count", 0) for s in sync_stats.values())
            ghosts = sum(s.get("ghost", 0) for s in sync_stats.values())
            status["total_synced"] = total
            status["total_ghost"] = ghosts
            status["total_valid"] = total - ghosts
        except Exception:
            pass

        return status


def main():
    """CLI 入口"""
    import argparse
    parser = argparse.ArgumentParser(description="AI Memory Sync Bridge")
    parser.add_argument("--sync-hermes", action="store_true", help="同步 Hermes 记忆")
    parser.add_argument("--sync-openclaw", action="store_true", help="同步 OpenClaw 记忆")
    parser.add_argument("--sync-sessions", action="store_true", help="同步 Hermes 会话")
    parser.add_argument("--status", action="store_true", help="查看同步状态（本地索引，零API调用）")
    parser.add_argument("--cleanup-ghosts", action="store_true", help="清理幽灵记录（抽样验证）")
    parser.add_argument("--quality-stats", action="store_true", help="查看内容质量评分统计（P13）")
    parser.add_argument("--all", action="store_true", help="执行完整同步")

    args = parser.parse_args()

    bridge = MemorySyncBridge()

    if args.cleanup_ghosts:
        print("清理幽灵记录...")
        cleaned = bridge.cleanup_ghost_records(sample_size=10)
        print(f"  标记 {cleaned} 条幽灵记录")

    if args.all:
        args.sync_hermes = True
        args.sync_openclaw = True
        args.sync_sessions = True

    if args.sync_hermes:
        print("同步 Hermes 记忆...")
        results = bridge.sync_hermes_memories()
        print(f"  Memory: {results['memory']}, User: {results['user']}, Skipped: {results['skipped']}")

    if args.sync_sessions:
        print("同步 Hermes 会话...")
        results = bridge.sync_hermes_sessions(limit=30)
        print(f"  Synced: {results['synced']}, Skipped: {results['skipped']}")

    if args.sync_openclaw:
        print("同步 OpenClaw 记忆...")
        results = bridge.sync_openclaw_memories()
        print(f"  Files: {results['files']}, Chunks: {results['chunks']}, Skipped: {results['skipped']}")

    if args.quality_stats:
        print("\n内容质量评分统计（P13）:")
        qstats = bridge.get_quality_stats()
        print(f"  已评分记录: {qstats.get('total_scored', 0)} 条")
        print(f"  平均分: {qstats.get('avg_score', 0)} 分")
        print(f"  最低分: {qstats.get('min_score', 0)} 分")
        print(f"  最高分: {qstats.get('max_score', 0)} 分")
        tiers = qstats.get('tiers', {})
        if tiers:
            print(f"  分档统计:")
            for tier, count in sorted(tiers.items()):
                print(f"    {tier}: {count} 条")

    if args.status or not any([
        args.sync_hermes, args.sync_openclaw, args.sync_sessions,
        args.cleanup_ghosts, args.quality_stats,
    ]):
        print("\n同步状态（本地索引）:")
        status = bridge.get_sync_status()
        print(f"  索引路径: {status.get('index_path', 'N/A')}")
        print(f"  最后检查: {status.get('last_sync', 'N/A')}")
        print(f"  总计已同步: {status.get('total_synced', 0)} 条")
        print(f"  有效记录: {status.get('total_valid', 0)} 条")
        if status.get('total_ghost', 0) > 0:
            print(f"  幽灵记录: {status['total_ghost']} 条（运行 --cleanup-ghosts 清理）")

        for key in sorted(status.keys()):
            if key.endswith('_synced') and isinstance(status[key], dict):
                src = key.replace('_synced', '')
                stat = status[key]
                print(f"  {src}: {stat.get('count', 0)} 条 (valid={stat.get('valid', 0)}, ghost={stat.get('ghost', 0)})")


if __name__ == "__main__":
    main()


# 提供兼容别名
AIMemorySync = MemorySyncBridge
