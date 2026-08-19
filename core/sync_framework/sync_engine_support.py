# -*- coding: utf-8 -*-
"""Content formatting and storage/audit helpers for SyncEngine."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.config import ConfigProvider, get_config
from core.kia.ingest_helpers import is_noise_message
from core.task_id_parser import TagBuilder, TaskIdParser

from .agent_source import (
    AgentSource,
    BatchSyncResult,
    SessionInfo,
    SyncResult,
    Turn,
)
from .storage_backend import StorageBackend
from .sync_log_store import SyncLogStore

logger = logging.getLogger(__name__)


_SANITIZE_PATTERN_LOADER: Callable[[], List[tuple[str, str]]] | None = None


class BackendDuplicateStateUnavailableError(RuntimeError):
    """Backend deduplication evidence is unavailable, never an empty match set."""


def bind_sanitize_pattern_loader(
    loader: Callable[[], List[tuple[str, str]]],
) -> None:
    """Bind the facade-owned configuration reader without a reverse import."""
    global _SANITIZE_PATTERN_LOADER
    _SANITIZE_PATTERN_LOADER = loader


def sanitize_content(content: str) -> str:
    """脱敏处理 — 确保 CaptureService 和 SyncEngine 哈希一致"""
    if _SANITIZE_PATTERN_LOADER is None:
        raise RuntimeError("sanitize pattern loader is not bound")
    for pattern, replacement in _SANITIZE_PATTERN_LOADER():
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
    lines.extend(
        [
            f"## {title}",
            "",
            "````json",
            _json_dumps(value),
            "````",
            "",
        ]
    )


def _append_text_section(lines: List[str], title: str, text: str):
    if not text:
        return
    lines.extend(
        [
            f"## {title}",
            "",
            text,
            "",
        ]
    )


def _get_reasoning_mode() -> str:
    try:
        # type: ignore[no-any-return]
        return get_config().get("capture.reasoning_mode", "artifact_summary")  # type: ignore[no-any-return]  # noqa: E501
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
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

    # 结构化对话证据必须进入投影层，否则 parser 已采到的信息会在 Obsidian 可见层丢失。
    _append_json_section(lines, "Tool Calls", turn.tool_calls)
    _append_json_section(lines, "Tool Results", turn.tool_results)
    _append_json_section(lines, "Attachments", turn.attachments)

    reasoning_mode = _get_reasoning_mode()
    metadata = turn.metadata or {}
    reasoning_text = turn.reasoning or metadata.get("reasoning", "")
    reasoning_artifact = metadata.get("reasoning_artifact_path") or metadata.get("artifact_path")
    reasoning_hash = metadata.get("reasoning_sha256")
    if reasoning_text and not reasoning_hash:
        reasoning_hash = hashlib.sha256(reasoning_text.encode("utf-8")).hexdigest()

    if reasoning_text or reasoning_artifact or reasoning_hash:
        if reasoning_mode == "full":
            _append_text_section(lines, "Reasoning", reasoning_text)
        elif reasoning_mode == "summary":
            summary = reasoning_text
            if len(summary) > 2000:
                summary = (
                    summary[:2000]
                    + "\n\n[... reasoning summary truncated by capture.reasoning_mode=summary ...]"
                )
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
        _append_text_section(
            lines, "Capture Artifact", f"Full oversized payload: `{artifact_path}`"
        )

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
    return hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


class SyncEngineSupportMixin:
    """Storage formatting, tag, duplicate, and audit operations."""

    # Structural contract supplied by ``SyncEngine``.  Keeping it explicit
    # makes the extracted support module type-safe without creating a runtime
    # compatibility base class.
    config: ConfigProvider
    backend: StorageBackend
    _pool: Any
    _sync_log: SyncLogStore
    canonicalize_session_info: Callable[[SessionInfo], SessionInfo]

    def sync_session(
        self,
        source: AgentSource,
        session_info: SessionInfo,
        incremental: bool = True,
    ) -> List[SyncResult]:
        """Structural method implemented by the concrete SyncEngine."""
        raise NotImplementedError

    def _is_noise(self, turn: Turn) -> bool:
        """噪音过滤"""
        combined = f"{turn.user_content}\n{turn.assistant_content}"
        return is_noise_message(combined)

    def _build_markdown(self, turn: Turn, session_id: str, model_tag: str) -> str:
        """将 Turn 构建为 Markdown 内容"""
        return build_turn_markdown(turn, session_id, model_tag)

    def _sanitize_content(self, content: str) -> str:
        """脱敏处理 — 复用统一脱敏规则"""
        return sanitize_content(content)

    def _build_tags(
        self,
        source: AgentSource,
        turn: Turn,
        session_info: SessionInfo,
    ) -> List[str]:
        """构建七维标签 + 自动检测"""
        # 解析 task_id（从用户消息中）
        task_id = TaskIdParser.parse(turn.user_content)
        # Captured conversations are user-derived cognition.  A lack of an
        # explicit private keyword is not consent to cross-agent publication;
        # the source/session tags below provide the private retrieval scope.
        scope = "private"

        tags = TagBuilder.build_tags(
            source=source.name,
            model=source.model_tag,
            task_id=task_id,  # type: ignore[arg-type]
            scope=scope,
        )

        # 七维标签补充
        tags.append("status=raw")
        tags.append("content_type=session-record")
        tags.append("layer=L1")
        tags.append(f"session={session_info.session_id}")
        if session_info.session_aliases:
            for alias in session_info.session_aliases[:3]:
                if alias != session_info.session_id:
                    tags.append(f"session_alias={alias}")
        if session_info.source_kind:
            tags.append(f"source_kind={session_info.source_kind}")
        tags.append(f"turn={turn.turn_number + 1}")

        # 自动检测标签
        combined = f"{turn.user_content}\n{turn.assistant_content}"
        if "```" in combined:
            tags.append("has-code=true")
        if (
            "[TOOL_RESULT]" in combined
            or turn.metadata.get("tool_calls")
            or turn.tool_calls
            or turn.tool_results
        ):
            tags.append("has-tools=true")
        if (
            turn.reasoning
            or turn.metadata.get("reasoning")
            or turn.metadata.get("reasoning_artifact_path")
            or turn.metadata.get("reasoning_sha256")
        ):
            tags.append("has-reasoning=true")
            tags.append(
                f"reasoning_capture={self.config.get('capture.reasoning_mode', 'artifact_summary')}"
            )

        # P0-0: 完整性标签写入 StorageBackend
        comp = turn.completeness or {}
        tags.append(f"capture_visible={comp.get('visible_text', 'unknown')}")
        if comp.get("tool_results") and comp.get("tool_results") != "unavailable":
            tags.append(f"capture_tool_results={comp.get('tool_results')}")
        if comp.get("reasoning") and comp.get("reasoning") != "unavailable":
            tags.append(f"capture_reasoning={comp.get('reasoning')}")
        if comp.get("truncated"):
            tags.append("capture_truncated=true")
            artifact_digest = str(
                (turn.metadata or {}).get("capture_artifact_sha256") or ""
            )
            if (
                (turn.metadata or {}).get("artifact_path")
                and len(artifact_digest) == 64
                and all(character in "0123456789abcdef" for character in artifact_digest)
            ):
                tags.append(f"capture_artifact_sha256={artifact_digest}")
        if comp.get("loss_reasons"):
            tags.append(f"capture_loss={','.join(comp.get('loss_reasons', [])[:3])}")

        # 回流防护：wiki 生成内容不蒸馏
        if "<wiki-context" in combined or "<!-- wiki-generated -->" in combined:
            tags.append("skip-distill=true")

        return tags

    def _save_content(self, content: str, tags: List[str], title: str):
        """保存内容到后端（分片策略由后端自行决定）"""
        return self.backend.save(content=content, tags=tags, title=title)

    def _get_last_synced_turn(self, agent_name: str, session_id: str) -> int:
        """获取上次成功同步到的轮次号（排除 failed，确保失败记录下次可被重试）"""
        return self._sync_log.last_synced_turn(agent_name, session_id)

    def _get_synced_turns(self, agent_name: str, session_id: str) -> List[int]:
        """获取某 session 已同步的所有 turn_number 列表（P0-4 backfill 缺洞检测）"""
        return self._sync_log.synced_turns(agent_name, session_id)

    def get_synced_turns_for_session(
        self,
        agent_name: str,
        session_info: SessionInfo,
    ) -> List[int]:
        """Read sync-log state through the same canonical-session resolver as Raw."""
        canonical = self.canonicalize_session_info(session_info)
        return self._get_synced_turns(agent_name, canonical.session_id)

    def record_audit(
        self,
        source: str,
        audit_type: str,
        skipped_missing: int = 0,
        skipped_large: int = 0,
        skipped_stale: int = 0,
        skipped_unchanged: int = 0,
        skipped_over_limit: int = 0,
        selected: int = 0,
        synced_turns: int = 0,
    ) -> None:
        """[P0-1] 记录扫描审计统计"""
        self._sync_log.record_audit(
            source,
            audit_type,
            skipped_missing=skipped_missing,
            skipped_large=skipped_large,
            skipped_stale=skipped_stale,
            skipped_unchanged=skipped_unchanged,
            skipped_over_limit=skipped_over_limit,
            selected=selected,
            synced_turns=synced_turns,
        )

    def get_audit_summary(self, hours: int = 24) -> Dict[str, Dict[str, int]]:
        """[P0-1] 获取最近 N 小时的审计摘要，按 source 分组"""
        return self._sync_log.audit_summary(hours)

    def _check_synced(self, agent_name: str, session_id: str, turn_number: int) -> Optional[Dict]:
        """检查某轮次是否已同步"""
        return self._sync_log.synced_record(agent_name, session_id, turn_number)

    def _check_backend_duplicate(
        self, agent_name: str, session_id: str, turn_number: int, content_hash: str
    ) -> List[str]:
        """查询后端是否已有相同 session+turn+content 的记录 — 兜底防重"""
        try:
            tags = [
                f"source={agent_name}",
                f"session={session_id}",
                f"turn={turn_number + 1}",
            ]
            results = self.backend.list_by_tags(tags, limit=5)
            matched = []
            for r in results:
                if f"content_hash={content_hash}" in r.tags:
                    matched.append(r.uid)
            return matched
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logging.getLogger(__name__).warning(
                "[SyncEngine] backend duplicate lookup unavailable",
                exc_info=True,
            )
            raise BackendDuplicateStateUnavailableError(
                "backend_duplicate_lookup_unavailable"
            ) from None

    def build_backend_duplicate_cache(
        self, agent_name: str
    ) -> Optional[Dict[Tuple[str, int, str], List[str]]]:
        """按 Agent 一次性构建后端去重缓存，供历史回填避免每 turn 拉全库。"""
        try:
            memories = self.backend.list_by_tags([f"source={agent_name}"], limit=None)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.warning("[SyncEngine] 构建后端去重缓存失败", exc_info=True)
            raise BackendDuplicateStateUnavailableError(
                "backend_duplicate_cache_unavailable"
            ) from None

        cache: Dict[Tuple[str, int, str], List[str]] = {}
        for memory in memories or []:
            tags = list(getattr(memory, "tags", []) or [])
            session_id = None
            turn_number = None
            hashes: List[str] = []

            for tag in tags:
                if tag.startswith("session="):
                    session_id = tag.split("=", 1)[1]
                elif tag.startswith("turn="):
                    try:
                        turn_number = int(tag.split("=", 1)[1]) - 1
                    except ValueError:
                        turn_number = None
                elif tag.startswith("content_hash="):
                    hashes.append(tag.split("=", 1)[1])

            if session_id is None or turn_number is None:
                continue

            if not hashes:
                body = (getattr(memory, "content", "") or "").strip()
                hashes = [hashlib.md5(body.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]]

            uid = getattr(memory, "uid", None)
            if not uid:
                continue
            for content_hash in hashes:
                key = (session_id, turn_number, content_hash)
                cache.setdefault(key, []).append(uid)

        return cache

    def _record_sync(
        self,
        agent_name: str,
        session_id: str,
        turn_number: int,
        content_hash: str,
        backend_uids: List[str],
        status: str,
        error: Optional[str] = None,
        artifact_path: Optional[str] = None,
    ):
        """记录同步状态（含蒸馏扩展字段）"""
        self._sync_log.record_sync(
            agent_name,
            session_id,
            turn_number,
            content_hash,
            backend_uids,
            status,
            error=error,
            artifact_path=artifact_path,
        )

    def _get_failed_records(self, agent_name: Optional[str], limit: int) -> List[Dict]:
        """获取失败的同步记录"""
        return self._sync_log.failed_records(agent_name, limit)

    def _get_source(self, agent_name: str) -> Optional[AgentSource]:
        """获取 AgentSource 实例"""
        from .registry import SourceRegistry

        return SourceRegistry.get(agent_name)

    def _make_sync_record_tuple(
        self,
        agent_name: str,
        session_id: str,
        turn_number: int,
        content_hash: str,
        backend_uids: List[str],
        status: str,
        error: Optional[str],
        artifact_path: Optional[str],
    ) -> tuple:
        return (
            agent_name,
            session_id,
            turn_number,
            content_hash,
            (
                json.dumps(backend_uids)
                if isinstance(backend_uids, list)
                else json.dumps([backend_uids])
            ),
            status,
            datetime.now().isoformat(),
            "pending" if status in ("new", "updated") else "skipped",
            error,
            artifact_path,
        )

    def _record_sync_and_persona_batch(
        self,
        records: List[tuple],
        signals: List[tuple],
        *,
        existing_sync_bindings: List[tuple[str, str, int, str]] | None = None,
    ) -> frozenset[tuple[str, str, int]] | None:
        """Commit sync-log and exact persona projections in one SQLite unit."""
        from core.sync_framework.sync_persona_signals import (
            record_sync_and_persona_batch,
        )

        return record_sync_and_persona_batch(
            self._pool.get_conn,
            records,
            signals,
            existing_sync_bindings=existing_sync_bindings,
            config=self.config,
        )

    def _make_persona_signal_tuple(
        self,
        source: AgentSource,
        turn: Turn,
        session_id: str,
    ) -> tuple:
        combined = f"{turn.user_content}\n{turn.assistant_content}"
        return (
            datetime.now().isoformat(),
            source.name,
            session_id,
            turn.turn_number,
            len(combined),
            1 if "```" in combined else 0,
            1 if "[TOOL_RESULT]" in combined else 0,
            combined.count("?"),
        )

    def _check_synced_batch(
        self,
        agent_name: str,
        session_id: str,
        turn_numbers: List[int],
    ) -> Dict[int, Dict]:
        """批量检查多个 turn 的同步状态。"""
        return self._sync_log.synced_batch(agent_name, session_id, turn_numbers)

    def sync_batch(
        self,
        source: AgentSource,
        sessions: List[SessionInfo],
        incremental: bool = True,
    ) -> BatchSyncResult:
        """批量同步多个会话，支持部分成功。"""
        result = BatchSyncResult(
            agent=source.name,
            total_sessions=len(sessions),
        )

        unique_sessions: Dict[str, SessionInfo] = {}
        for session_info in sessions:
            canonical_info = self.canonicalize_session_info(session_info)
            existing = unique_sessions.get(canonical_info.session_id)
            if existing is None or (canonical_info.mtime or 0) > (existing.mtime or 0):
                unique_sessions[canonical_info.session_id] = canonical_info

        for session_info in unique_sessions.values():
            try:
                results = self.sync_session(source, session_info, incremental)
                session_summary = {
                    "session_id": session_info.session_id,
                    "source_session_id": (
                        session_info.session_aliases or [session_info.session_id]
                    )[0],
                    "results": results,
                }
                result.successful.append(session_summary)

                for sync_result in results:
                    if sync_result.action in result.turn_stats:
                        result.turn_stats[sync_result.action] += 1

            except (
                ImportError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
                sqlite3.Error,
            ) as exc:
                logger.error(
                    "[SyncEngine] 批量同步 session 失败 %s: %s",
                    session_info.session_id,
                    exc,
                )
                result.failed.append(
                    {
                        "session_id": session_info.session_id,
                        "error": str(exc),
                    }
                )
                result.turn_stats["failed"] += 1

        return result

    def retry_failed(
        self,
        agent_name: Optional[str] = None,
        limit: int = 50,
    ) -> List[SyncResult]:
        """重试失败的同步记录，排除明确不可重试的认证错误。"""
        failed_records = self._get_failed_records(agent_name, limit)
        if not failed_records:
            return []

        results: List[SyncResult] = []
        for record in failed_records:
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
                results.extend(
                    self.sync_session(
                        source,
                        session_info,
                        incremental=False,
                    )
                )
            except (
                ImportError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
                sqlite3.Error,
            ) as exc:
                logger.error(
                    "[SyncEngine] 重试失败 %s: %s",
                    record["session_id"],
                    exc,
                    exc_info=True,
                )

        return results
