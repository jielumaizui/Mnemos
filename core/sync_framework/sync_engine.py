# -*- coding: utf-8 -*-
"""
SyncEngine — 统一同步协调层

AgentSource 和 MemosClient 之间的统一协调层。
插件只负责：发现会话 + 解析消息。
引擎负责：增量跳过→噪音过滤→内容构建→脱敏→去重→标签组装→存储分片→信号采集。

8 步流水线:
  1. 增量跳过 — 基于 turn_number 跳过已同步轮次
  2. 噪音过滤 — 统一 is_noise_message()
  3. 内容构建 — Markdown 格式化
  4. 脱敏 — 复用 MemosClient._sanitize()
  5. 去重检查 — content_hash 对比
  6. 标签组装 — 七维标签 + 插件扩展 + 自动检测
  7. 存储分片 — Config 驱动阈值，超长自动分片
  8. 信号采集 — 画像行为信号 + sync_log 状态记录
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

from integrations.styx import (
    MemosClient,
    MemosRateLimitError,
    MemosAuthError,
    MemosServerError,
)
from core.config import get_config
from core.db_utils import SqlitePool
from core.kia.ingest_helpers import is_noise_message
from core.task_id_parser import TagBuilder, TaskIdParser

from .agent_source import AgentSource, SessionInfo, Turn, SyncResult, BatchSyncResult


# ========== 模块级辅助函数：统一 content_hash 计算 ==========

_DEFAULT_SANITIZE_PATTERNS = [
    (r'sk-[a-zA-Z0-9]{20,}', '[API-KEY]'),
    (r'gh[pousr]_[A-Za-z0-9_]{36,}', '[GITHUB-TOKEN]'),
    (r'AKID[0-9a-zA-Z]{10,}', '[CLOUD-KEY]'),
    (r'password[:=]\s*\S+', 'password=[HIDDEN]'),
    (r'secret[:=]\s*\S+', 'secret=[HIDDEN]'),
    (r'token[:=]\s*\S+', 'token=[HIDDEN]'),
]


def _load_sanitize_patterns():
    """从配置文件加载脱敏规则，不存在则用内置默认值"""
    cfg_dir = Path.home() / ".mnemos" / "configs"
    patterns_file = cfg_dir / "sanitize_patterns.json"
    if patterns_file.exists():
        try:
            with open(patterns_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            patterns = []
            for item in data:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    patterns.append((item[0], item[1]))
            if patterns:
                return patterns
        except Exception:
            pass
    return list(_DEFAULT_SANITIZE_PATTERNS)


def sanitize_content(content: str) -> str:
    """脱敏处理 — 不依赖 MemosClient 实例，确保 CaptureService 和 SyncEngine 哈希一致"""
    for pattern, replacement in _load_sanitize_patterns():
        content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)
    return content


def _json_dumps(value: Any) -> str:
    """稳定渲染结构化采集字段，避免不同路径生成不同 content_hash。"""
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    except TypeError:
        return json.dumps(str(value), ensure_ascii=False)


def _append_json_section(lines: List[str], title: str, value: Any):
    if not value:
        return
    lines.extend([
        f"## {title}",
        "",
        "````json",
        _json_dumps(value),
        "````",
        "",
    ])


def _append_text_section(lines: List[str], title: str, text: str):
    if not text:
        return
    lines.extend([
        f"## {title}",
        "",
        text,
        "",
    ])


def _get_reasoning_mode() -> str:
    try:
        return get_config().get("capture.reasoning_mode", "artifact_summary")
    except Exception:
        return "artifact_summary"


def build_turn_markdown(turn: Turn, session_id: str, model_tag: str) -> str:
    """将 Turn 构建为 Markdown 内容"""
    lines = [
        f"## Turn {turn.turn_number + 1}",
        "",
        f"**User** ({model_tag}):",
        "",
        turn.user_content,
        "",
        "**Assistant**:",
        "",
        turn.assistant_content,
        "",
    ]

    # 结构化对话证据必须进入投影层，否则 parser 已采到的信息会在 Memos/Obsidian 可见层丢失。
    _append_json_section(lines, "Tool Calls", turn.tool_calls)
    _append_json_section(lines, "Tool Results", turn.tool_results)
    _append_json_section(lines, "Attachments", turn.attachments)

    reasoning_mode = _get_reasoning_mode()
    metadata = turn.metadata or {}
    reasoning_text = turn.reasoning or metadata.get("reasoning", "")
    reasoning_artifact = metadata.get("reasoning_artifact_path") or metadata.get("artifact_path")
    reasoning_hash = metadata.get("reasoning_sha256")
    if reasoning_text and not reasoning_hash:
        reasoning_hash = hashlib.sha256(reasoning_text.encode("utf-8")).hexdigest()[:16]

    if reasoning_text or reasoning_artifact or reasoning_hash:
        if reasoning_mode == "full":
            _append_text_section(lines, "Reasoning", reasoning_text)
        elif reasoning_mode == "summary":
            summary = reasoning_text
            if len(summary) > 2000:
                summary = summary[:2000] + "\n\n[... reasoning summary truncated by capture.reasoning_mode=summary ...]"
            _append_text_section(lines, "Reasoning Summary", summary)
        elif reasoning_mode == "artifact_summary":
            note = "Reasoning captured; full content is stored as a local artifact."
            if reasoning_hash:
                note += f"\n\nChecksum: `{reasoning_hash}`"
            if reasoning_artifact:
                note += f"\n\nArtifact: `{reasoning_artifact}`"
            _append_text_section(lines, "Reasoning", note)

    artifact_path = (turn.metadata or {}).get("artifact_path")
    if artifact_path and artifact_path != (turn.metadata or {}).get("reasoning_artifact_path"):
        _append_text_section(lines, "Capture Artifact", f"Full oversized payload: `{artifact_path}`")

    lines.extend(["---", ""])
    return "\n".join(lines)


def compute_content_hash(
    user_content: str,
    assistant_content: str,
    turn_number: int,
    model_tag: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    tool_results: Optional[List[Dict[str, Any]]] = None,
    reasoning: str = "",
    attachments: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """
    统一 content_hash 计算函数。
    CaptureService 和 SyncEngine 必须复用同一函数，确保 sync_log 去重兜底有效。
    """
    turn = Turn(
        turn_number=turn_number,
        user_content=user_content or "",
        assistant_content=assistant_content or "",
        metadata=metadata or {},
        tool_calls=tool_calls or [],
        tool_results=tool_results or [],
        reasoning=reasoning or "",
        attachments=attachments or [],
    )
    content = build_turn_markdown(turn, "", model_tag)
    content = sanitize_content(content)
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:16]


class SyncEngine:
    """
    AgentSource 和 MemosClient 之间的统一协调层。

    设计原则：
    - 插件不可见内部逻辑，只提供原始数据
    - 所有同步数据统一经过此引擎，不绕路
    - 画像信号在同步成功后统一采集
    - 统一防重：一个 SQLite 库管所有 Agent
    """

    def __init__(
        self,
        client: Optional[MemosClient] = None,
        db_path: Optional[str] = None,
    ):
        self.config = get_config()
        self.client = client or self._build_default_client()
        self.db_path = Path(db_path or self.config.data_dir / "sync_log.db").expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._shard_threshold = self.config.get("memos.max_content_bytes", 7792)
        self._pool = SqlitePool(self.db_path)
        self._init_db()

    def close(self):
        """关闭持久连接"""
        if hasattr(self, '_pool'):
            self._pool.close()

    # ---------- 内部工厂 ----------

    def _build_default_client(self) -> MemosClient:
        return MemosClient(
            token=self.config.memos_token,
            base_url=self.config.memos_api_url,
            agent="sync-engine",
        )

    def _init_db(self):
        """初始化统一防重数据库"""
        conn = self._pool.get_conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_number INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                memos_uids TEXT,
                status TEXT DEFAULT 'synced',
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                distill_status TEXT DEFAULT 'pending',
                distill_job_id TEXT,
                distilled_at TIMESTAMP,
                wiki_page_paths TEXT,
                distill_error TEXT,
                error TEXT,
                working_dir TEXT,
                tags TEXT,
                artifact_path TEXT,
                UNIQUE(agent_name, session_id, turn_number)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sync_lookup
            ON sync_log(agent_name, session_id, turn_number)
        """)
        # 向后兼容：为旧数据库添加 artifact_path 列
        try:
            cursor.execute("SELECT artifact_path FROM sync_log LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE sync_log ADD COLUMN artifact_path TEXT")
            conn.commit()
        # 向后兼容：为旧数据库添加 error 列
        try:
            cursor.execute("SELECT error FROM sync_log LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE sync_log ADD COLUMN error TEXT")
            conn.commit()
        # 向后兼容：为旧数据库添加 persona_collected 列（画像信号采集用）
        try:
            cursor.execute("SELECT persona_collected FROM sync_log LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE sync_log ADD COLUMN persona_collected INTEGER DEFAULT 0")
            conn.commit()
        # 向后兼容：为旧数据库添加 working_dir 列
        try:
            cursor.execute("SELECT working_dir FROM sync_log LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE sync_log ADD COLUMN working_dir TEXT")
            conn.commit()
        # 向后兼容：为旧数据库添加 tags 列（画像信号采集用）
        try:
            cursor.execute("SELECT tags FROM sync_log LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE sync_log ADD COLUMN tags TEXT")
            conn.commit()
        # 画像信号表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_number INTEGER,
                content_length INTEGER,
                has_code INTEGER,
                has_tools INTEGER,
                user_questions INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        # 启动时清理旧 sync_log，防止表无限增长
        self._cleanup_old_sync_log()

    def _cleanup_old_sync_log(self, days: int = 90):
        """清理超过 N 天的已同步记录"""
        try:
            conn = self._pool.get_conn()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor = conn.execute(
                "DELETE FROM sync_log WHERE synced_at < ? AND status = 'synced'",
                (cutoff,)
            )
            conn.commit()
            if cursor.rowcount > 0:
                logger.info(f"[SyncEngine] 清理 {cursor.rowcount} 条旧 sync_log")
        except Exception:
            pass

    # ---------- 公共 API ----------

    def sync_single_turn(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        turn: Turn,
        incremental: bool = True,
    ) -> SyncResult:
        """
        同步单轮对话。

        供 CaptureWorker 调用，复用完整的 8 步流水线。
        """
        last_synced = self._get_last_synced_turn(source.name, session_info.session_id)

        # 1. 增量跳过
        if incremental and turn.turn_number < last_synced:
            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action="skipped",
            )

        # 2. 噪音过滤
        if self._is_noise(turn):
            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action="noise",
            )

        self._ensure_reasoning_artifact(turn, session_info.session_id)

        # 3. 内容构建
        content = self._build_markdown(turn, session_info.session_id, source.model_tag)

        # 4. 脱敏
        content = self._sanitize_content(content)

        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]

        # 5. 去重检查（本地 sync_log）
        existing = self._check_synced(
            source.name, session_info.session_id, turn.turn_number
        )
        if existing and existing.get("content_hash") == content_hash:
            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action="skipped",
                content_hash=content_hash,
            )

        # 5b. 去重检查（Memos 端兜底）— 防止 sync_log 丢失导致全量重同步
        # 从 Turn metadata 获取 artifact_path（由 CaptureService 写入）
        artifact_path = (turn.metadata or {}).get("artifact_path", "") or (turn.metadata or {}).get("reasoning_artifact_path", "")

        memos_dupe = self._check_memos_duplicate(
            source.name, session_info.session_id, turn.turn_number, content_hash
        )
        if memos_dupe:
            # 记录到 sync_log 防止下次再查
            self._record_sync(
                source.name, session_info.session_id,
                turn.turn_number, content_hash, memos_dupe, "skipped_memos",
                artifact_path=artifact_path,
            )
            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action="skipped",
                memos_uids=memos_dupe,
                content_hash=content_hash,
            )

        # 6. 标签组装
        tags = self._build_tags(source, turn, session_info)
        tags.extend(source.build_extra_tags(turn))
        # 6b. content_hash 标签：Memos 端兜底去重用
        tags.append(f"content_hash={content_hash}")

        # 7. 存储 + 分片
        title = f"{source.name}-{session_info.session_id[:8]}-turn{turn.turn_number + 1}"
        try:
            memories = self._save_content(content, tags, title)
            uids = [m.uid for m in memories] if memories else []
            status_str = "updated" if existing else "new"

            # 8. 状态记录 + 信号采集
            self._record_sync(
                source.name, session_info.session_id,
                turn.turn_number, content_hash, uids, status_str,
                artifact_path=artifact_path,
            )
            self._collect_persona_signal(source, turn, session_info.session_id)

            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action=status_str,
                memos_uids=uids,
                content_hash=content_hash,
            )
        except MemosRateLimitError as e:
            err_msg = f"rate_limit: {e}"
            logger.warning(f"[SyncEngine] 速率限制: {e}")
            self._record_sync(
                source.name, session_info.session_id,
                turn.turn_number, content_hash, [], "failed", error=err_msg,
                artifact_path=artifact_path,
            )
            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action="failed",
                content_hash=content_hash,
                error=err_msg,
            )
        except MemosAuthError as e:
            err_msg = f"auth_error: {e}"
            logger.error(f"[SyncEngine] 认证失败: {e}")
            self._record_sync(
                source.name, session_info.session_id,
                turn.turn_number, content_hash, [], "failed", error=err_msg,
                artifact_path=artifact_path,
            )
            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action="failed",
                content_hash=content_hash,
                error=err_msg,
            )
        except MemosServerError as e:
            err_msg = f"server_error: {e}"
            logger.warning(f"[SyncEngine] 服务器错误: {e}")
            self._record_sync(
                source.name, session_info.session_id,
                turn.turn_number, content_hash, [], "failed", error=err_msg,
                artifact_path=artifact_path,
            )
            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action="failed",
                content_hash=content_hash,
                error=err_msg,
            )
        except Exception as e:
            err_msg = str(e)
            logger.error(f"[SyncEngine] 同步失败: {e}")
            self._record_sync(
                source.name, session_info.session_id,
                turn.turn_number, content_hash, [], "failed", error=err_msg,
                artifact_path=artifact_path,
            )
            return SyncResult(
                session_id=session_info.session_id,
                turn_number=turn.turn_number,
                action="failed",
                content_hash=content_hash,
                error=err_msg,
            )

    def sync_session(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        incremental: bool = True,
    ) -> List[SyncResult]:
        """
        同步单个会话的所有轮次。

        Args:
            source: AgentSource 实例
            session_info: 会话信息
            incremental: 是否增量同步（只同步新增轮次）

        Returns:
            SyncResult 列表
        """
        turns = source.parse_turns(session_info.source_path)
        results: List[SyncResult] = []

        # 发射 polled 事件
        try:
            from core.mnemos_bus import publish_event
            publish_event("polled", source.name, {
                "file_path": str(session_info.source_path),
                "session_id": session_info.session_id,
            })
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at sync_engine.py", exc_info=True)
            pass

        # KIA Hook: session_start
        context = source.on_session_start(
            session_info.session_id,
            {"working_dir": session_info.working_dir, "agent": source.name},
        )

        for turn in turns:
            result = self.sync_single_turn(source, session_info, turn, incremental)
            # 增量跳过时不加入结果（保持原有行为）
            if incremental and result.action == "skipped" and result.content_hash is None:
                continue
            results.append(result)

        # KIA Hook: session_end
        all_messages = [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": t.user_content if i % 2 == 0 else t.assistant_content}
            for i, t in enumerate(turns)
        ]
        source.on_session_end(session_info.session_id, all_messages)

        return results

    def sync_batch(
        self,
        source: AgentSource,
        sessions: List[SessionInfo],
        incremental: bool = True,
    ) -> BatchSyncResult:
        """
        批量同步多个会话，支持部分成功。

        Args:
            source: AgentSource 实例
            sessions: 会话列表
            incremental: 是否增量同步

        Returns:
            BatchSyncResult — 批量同步结果，含成功/失败/跳过统计
        """
        result = BatchSyncResult(
            agent=source.name,
            total_sessions=len(sessions),
        )

        for session_info in sessions:
            try:
                results = self.sync_session(source, session_info, incremental)
                session_summary = {
                    "session_id": session_info.session_id,
                    "results": results,
                }
                result.successful.append(session_summary)

                for r in results:
                    if r.action in result.turn_stats:
                        result.turn_stats[r.action] += 1

            except Exception as e:
                logger.error(f"[SyncEngine] 批量同步 session 失败 {session_info.session_id}: {e}")
                result.failed.append({
                    "session_id": session_info.session_id,
                    "error": str(e),
                })
                result.turn_stats["failed"] += 1

        return result

    def retry_failed(self, agent_name: Optional[str] = None, limit: int = 50) -> List[SyncResult]:
        """
        重试失败的同步记录。

        扫描 sync_log 中 status='failed' 的记录，重新同步。
        仅重试可重试类型的错误（排除 auth_error）。

        Args:
            agent_name: 指定 Agent 重试，None 则重试所有
            limit: 最大重试数

        Returns:
            重试结果列表
        """
        failed_records = self._get_failed_records(agent_name, limit)
        if not failed_records:
            return []

        results = []
        for record in failed_records:
            # auth_error 不重试
            if record.get("error", "").startswith("auth_error:"):
                continue

            source = self._get_source(record["agent_name"])
            if not source:
                continue

            session_info = SessionInfo(
                session_id=record["session_id"],
                source_path=Path(record.get("source_path", "")),
            )
            try:
                # 重试失败记录时使用全量同步，避免 last_synced 跳过
                session_results = self.sync_session(source, session_info, incremental=False)
                results.extend(session_results)
            except Exception as e:
                logger.error(f"[SyncEngine] 重试失败 {record['session_id']}: {e}")

        return results

    # ---------- 流水线步骤 ----------

    def _is_noise(self, turn: Turn) -> bool:
        """噪音过滤"""
        combined = f"{turn.user_content}\n{turn.assistant_content}"
        return is_noise_message(combined)

    def _build_markdown(self, turn: Turn, session_id: str, model_tag: str) -> str:
        """将 Turn 构建为 Markdown 内容"""
        return build_turn_markdown(turn, session_id, model_tag)

    def _sanitize_content(self, content: str) -> str:
        """脱敏处理 — 复用 MemosClient 的规则"""
        return sanitize_content(content)

    def _ensure_reasoning_artifact(self, turn: Turn, session_id: str):
        """默认把完整 reasoning 留在本地 artifact，而不是塞满 Memos 正文。"""
        mode = self.config.get("capture.reasoning_mode", "artifact_summary")
        if mode != "artifact_summary" or not turn.reasoning:
            return

        metadata = turn.metadata or {}
        metadata["reasoning_sha256"] = hashlib.sha256(turn.reasoning.encode("utf-8")).hexdigest()[:16]
        if metadata.get("reasoning_artifact_path"):
            return

        data_dir = getattr(self.config, "data_dir", None) or self.config.get("data_dir", Path.home() / ".mnemos")
        artifact_dir = Path(data_dir) / "capture_artifacts" / session_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / f"turn_{turn.turn_number}_reasoning.md"
        content = "\n".join([
            "# Reasoning Artifact",
            "",
            f"- session_id: {session_id}",
            f"- turn_number: {turn.turn_number}",
            f"- captured_at: {datetime.now().isoformat()}",
            "",
            "---",
            "",
            turn.reasoning,
            "",
        ])
        path.write_text(content, encoding="utf-8")
        metadata["reasoning_artifact_path"] = str(path)
        turn.metadata = metadata
        if turn.completeness is not None:
            turn.completeness["reasoning"] = "artifact"

    def _build_tags(
        self,
        source: AgentSource,
        turn: Turn,
        session_info: SessionInfo,
    ) -> List[str]:
        """构建七维标签 + 自动检测"""
        # 解析 task_id（从用户消息中）
        task_id = TaskIdParser.parse(turn.user_content)
        is_private = TaskIdParser.is_private_request(turn.user_content)
        scope = "private" if is_private else "public"

        tags = TagBuilder.build_tags(
            source=source.name,
            model=source.model_tag,
            task_id=task_id,
            scope=scope,
        )

        # 七维标签补充
        tags.append("status=raw")
        tags.append("content_type=session-record")
        tags.append("layer=L1")
        tags.append(f"session={session_info.session_id}")
        tags.append(f"turn={turn.turn_number + 1}")

        # 自动检测标签
        combined = f"{turn.user_content}\n{turn.assistant_content}"
        if "```" in combined:
            tags.append("has-code=true")
        if "[TOOL_RESULT]" in combined or turn.metadata.get("tool_calls") or turn.tool_calls or turn.tool_results:
            tags.append("has-tools=true")
        if turn.reasoning or turn.metadata.get("reasoning") or turn.metadata.get("reasoning_artifact_path") or turn.metadata.get("reasoning_sha256"):
            tags.append("has-reasoning=true")
            tags.append(f"reasoning_capture={self.config.get('capture.reasoning_mode', 'artifact_summary')}")

        # P0-0: 完整性标签写入 Memos
        comp = turn.completeness or {}
        tags.append(f"capture_visible={comp.get('visible_text', 'unknown')}")
        if comp.get('tool_results') and comp.get('tool_results') != 'unavailable':
            tags.append(f"capture_tool_results={comp.get('tool_results')}")
        if comp.get('reasoning') and comp.get('reasoning') != 'unavailable':
            tags.append(f"capture_reasoning={comp.get('reasoning')}")
        if comp.get('truncated'):
            tags.append("capture_truncated=true")
        if comp.get('loss_reasons'):
            tags.append(f"capture_loss={','.join(comp.get('loss_reasons', [])[:3])}")

        # 回流防护：wiki 生成内容不蒸馏
        if "<wiki-context" in combined or "<!-- wiki-generated -->" in combined:
            tags.append("skip-distill=true")

        return tags

    def _save_content(self, content: str, tags: List[str], title: str):
        """保存内容到 Memos（含分片决策，阈值从 Config 读取）"""
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > self._shard_threshold:
            return self.client.save_long_content(
                content=content,
                tags=tags,
                visibility="PUBLIC",
                title=title,
                _trace_sync_log=False,
            )
        else:
            return [self.client.save(content, tags, "PUBLIC", _trace_sync_log=False)]

    def _collect_persona_signal(self, source: AgentSource, turn: Turn, session_id: str):
        """采集用户行为信号，供画像系统分析"""
        combined = f"{turn.user_content}\n{turn.assistant_content}"
        try:
            conn = self._pool.get_conn()
            conn.execute("""
                INSERT INTO user_signals
                (timestamp, agent, session_id, turn_number, content_length, has_code, has_tools, user_questions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(), source.name, session_id,
                turn.turn_number, len(combined),
                1 if "```" in combined else 0,
                1 if "[TOOL_RESULT]" in combined else 0,
                combined.count("?"),
            ))
            conn.commit()
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at sync_engine.py", exc_info=True)
            pass

    # ---------- 数据库操作 ----------

    def _get_last_synced_turn(self, agent_name: str, session_id: str) -> int:
        """获取上次同步到的轮次号"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT MAX(turn_number) FROM sync_log WHERE agent_name = ? AND session_id = ?",
                (agent_name, session_id),
            )
            row = cursor.fetchone()
            return (row[0] + 1) if row[0] is not None else 0
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at sync_engine.py", exc_info=True)
            return 0

    def _get_synced_turns(self, agent_name: str, session_id: str) -> List[int]:
        """获取某 session 已同步的所有 turn_number 列表（P0-4 backfill 缺洞检测）"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT turn_number FROM sync_log
                WHERE agent_name = ? AND session_id = ?
                  AND status IN ('new', 'updated', 'synced', 'skipped_memos')
                """,
                (agent_name, session_id),
            )
            return [row[0] for row in cursor.fetchall()]
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at sync_engine.py", exc_info=True)
            return []

    def _check_synced(self, agent_name: str, session_id: str, turn_number: int) -> Optional[Dict]:
        """检查某轮次是否已同步"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT content_hash, memos_uids, status FROM sync_log WHERE agent_name = ? AND session_id = ? AND turn_number = ?",
                (agent_name, session_id, turn_number),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "content_hash": row[0],
                    "memos_uids": json.loads(row[1]) if row[1] else [],
                    "status": row[2],
                }
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
        return None

    def _check_memos_duplicate(
        self, agent_name: str, session_id: str, turn_number: int, content_hash: str
    ) -> List[str]:
        """查询 Memos 是否已有相同 session+turn+content 的记录 — 兜底防重"""
        try:
            tags = [
                f"source={agent_name}",
                f"session={session_id}",
                f"turn={turn_number + 1}",
            ]
            results = self.client.list_by_tags(tags, limit=5)
            matched = []
            for r in results:
                # 优先检查 content_hash 标签（精确匹配，不受标签解析差异影响）
                if f"content_hash={content_hash}" in r.tags:
                    matched.append(r.uid)
                    continue
                # 兼容旧数据：没有 content_hash 标签时回退到 MD5 比较
                body = (r.content or "").strip()
                body_hash = hashlib.md5(body.encode("utf-8")).hexdigest()[:16]
                if body_hash == content_hash:
                    matched.append(r.uid)
            return matched
        except Exception:
            pass
        return []

    def _record_sync(
        self,
        agent_name: str,
        session_id: str,
        turn_number: int,
        content_hash: str,
        memos_uids: List[str],
        status: str,
        error: Optional[str] = None,
        artifact_path: Optional[str] = None,
    ):
        """记录同步状态（含蒸馏扩展字段）"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO sync_log
                (agent_name, session_id, turn_number, content_hash, memos_uids,
                 status, synced_at, distill_status, error, artifact_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent_name, session_id, turn_number, content_hash,
                json.dumps(memos_uids) if isinstance(memos_uids, list) else json.dumps([memos_uids]),
                status, datetime.now().isoformat(),
                "pending" if status in ("new", "updated") else "skipped",
                error,
                artifact_path,
            ))
            conn.commit()
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
    def _get_failed_records(self, agent_name: Optional[str], limit: int) -> List[Dict]:
        """获取失败的同步记录"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            if agent_name:
                cursor.execute(
                    "SELECT agent_name, session_id, turn_number, content_hash, error FROM sync_log WHERE status = 'failed' AND agent_name = ? ORDER BY synced_at DESC LIMIT ?",
                    (agent_name, limit),
                )
            else:
                cursor.execute(
                    "SELECT agent_name, session_id, turn_number, content_hash, error FROM sync_log WHERE status = 'failed' ORDER BY synced_at DESC LIMIT ?",
                    (limit,),
                )
            return [
                {"agent_name": r[0], "session_id": r[1], "turn_number": r[2], "content_hash": r[3], "error": r[4]}
                for r in cursor.fetchall()
            ]
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at sync_engine.py", exc_info=True)
            return []

    def _get_source(self, agent_name: str) -> Optional[AgentSource]:
        """获取 AgentSource 实例"""
        from .registry import AgentRegistry
        return AgentRegistry.get(agent_name)
