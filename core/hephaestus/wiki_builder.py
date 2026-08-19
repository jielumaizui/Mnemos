#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging

"""
Wiki Builder — L1 → Wiki 共享工具与批量回追入口

模块定位（2026-06 更新）：
    本模块提供 L1 原始会话重建、质量评分、Wiki 状态管理等**共享基础设施**，
    供 wiki_rebuild.py 等模块复用。`run_build_cycle()` 是**回追（catch-up）工具**，
    用于处理未进入 distill_queue 的 L1 记录；正常生产路径应使用 HephaestusWorker
    （distill_queue → 委托 → 收集 → 入库）。

共享设施（稳定 API）：
    - reconstruct_session()      — 从 L1 records 重建消息列表
    - score_session()            — 质量评分
    - fetch_l1_sessions()        — L1 查询与会话分组
    - _mark_processed()          — 处理状态追踪
    - _link_session_records_to_wiki() — L1-Wiki 映射
    - update_index_md()          — 索引更新
    - update_moc_pages()         — MOC 自动生成

回追入口（仅手动/定时触发）：
    - run_build_cycle()          — 扫描 L1 storage，批量蒸馏已完成 session
    - main()                     — CLI 入口

写模式：
    - create: 新建页面（默认）
    - merge: 合并到已有页面
    - incremental: 增量更新已有页面
"""

import sys
import hashlib
import json
import sqlite3
import time
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

logger = logging.getLogger(__name__)

WIKI_BUILDER_OPERATION_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    sqlite3.Error,
    subprocess.SubprocessError,
)

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from core.evidence.artifact_capture import (  # noqa: E402
    read_historical_capture_artifact_bytes,
    read_managed_capture_artifact_bytes,
)
from core.sync_framework.storage_backend import StorageBackend, create_storage_backend  # noqa: E402
from core.ops.durable_io import (  # noqa: E402
    read_native_bytes_with_metadata,
)
from core.config import get_config  # noqa: E402
from core.hephaestus.distillation_engine import (  # noqa: E402
    DistillationEngine,
    build_session_text,
    clean_message_content as _clean_for_distill,
)
from core.hephaestus.evolution_tracker import RecirculationGuard  # noqa: E402
from core.hephaestus.wiki_builder_support import (  # noqa: E402
    build_page_descriptor as _build_page_descriptor,
    filter_hot_pages as _filter_hot_pages,
    filter_low_confidence_pages as _filter_low_confidence_pages,
    filter_pending_pages as _filter_pending_pages,
    filter_recent_pages as _filter_recent_pages,
    parse_record_time as _parse_record_time,
)

# Phase-1: 复盘触发器 — session 跳过时自动创建复盘任务
from core.app.forced_retrospective import ForcedRetrospective  # noqa: E402

# Constants extracted from magic numbers
DURATION_BUCKET_WEEK_DAYS = 7
SEVEN_DAYS = 86400


def _get_wiki_dir() -> Path:
    return get_config().wiki_dir


def _get_wiki_db() -> Path:
    return get_config().database_dir / "wiki_state.db"


COMPLETION_TIMEOUT = 300  # 5分钟
MAX_SESSION_CHUNKS = 200


def _ensure_wiki_dirs():
    """确保 Wiki 目录结构存在"""
    for subdir in [
        "00-Inbox",
        "01-People",
        "02-Projects",
        "03-Tech",
        "04-Concepts",
        "05-MOCs",
        "06-Retrospectives",
    ]:
        (_get_wiki_dir() / subdir).mkdir(parents=True, exist_ok=True)


# ========== SQLite 状态管理 ==========


def _get_conn():
    db_path = _get_wiki_db()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_sessions (
            session_id TEXT PRIMARY KEY,
            source TEXT,
            message_count INTEGER,
            quality_score REAL,
            processed_at TEXT,
            distill_method TEXT,
            status TEXT DEFAULT 'pipeline'
        )
    """)
    # Explicit bootstrap ensures the current processed-session status column.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(processed_sessions)")}
    if "status" not in existing_cols:
        conn.execute("ALTER TABLE processed_sessions ADD COLUMN status TEXT DEFAULT 'pipeline'")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki_pages (
            page_id TEXT PRIMARY KEY,
            file_path TEXT,
            type TEXT,
            source_session TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    return conn


def _is_session_completed(session_id: str, records: List[Dict]) -> bool:
    """检测 session 是否已完成（最新 chunk 超过 5 分钟）"""
    if not records:
        return False
    latest_time = None
    for record in records:
        t = _parse_record_time(record)
        if t and (latest_time is None or t > latest_time):
            latest_time = t
    if not latest_time:
        return False
    # frontmatter 时间为本地时间（无 tz），与本地 now 比较；带 tz 的时间与 UTC now 比较
    if latest_time.tzinfo is None:
        now = datetime.now()
    else:
        now = datetime.now(timezone.utc)
    elapsed = (now - latest_time).total_seconds()
    return elapsed > COMPLETION_TIMEOUT


def _is_processed(session_id: str) -> bool:
    """检查 session 是否已处理过"""
    try:
        with _get_conn() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM processed_sessions WHERE session_id = ?",
                (session_id,),
            )
            return cursor.fetchone() is not None
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).warning(
            "Caught unexpected error at wiki_builder.py", exc_info=True
        )
        return False


def _mark_processed(
    session_id: str,
    source: str,
    message_count: int,
    quality_score: float,
    wiki_path: str = "",
    method: str = "pipeline",
    backend: StorageBackend | None = None,
    records: List[Dict] | None = None,
) -> None:
    """标记 session 已处理

    Args:
        backend: 可选的 StorageBackend，用于给 L1 记录添加 status=distilled 标签
        records: 该 session 的所有 L1 记录，用于提取 uid 更新标签
    """
    # status 与 distill_method 对齐，避免展示层误判
    status = (
        method
        if method
        in (
            "distilled",
            "skipped_low_quality",
            "skipped_distill",
            "recirculation_blocked",
            "skill_suggestion",
            "skipped_by_pipeline",
        )
        else "distilled"
    )
    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO processed_sessions
                   (session_id, source, message_count, quality_score,
                    processed_at, distill_method, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    source,
                    message_count,
                    quality_score,
                    datetime.now().isoformat(),
                    method,
                    status,
                ),
            )
            if wiki_path:
                conn.execute(
                    """INSERT OR REPLACE INTO wiki_pages
                       (page_id, file_path, type, source_session, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        wiki_path,
                        "source",
                        session_id,
                        datetime.now().isoformat(),
                        datetime.now().isoformat(),
                    ),
                )
            conn.commit()
    except (sqlite3.Error, OSError) as e:
        logger.warning("  [WikiBuilder] 标记处理状态失败: %s", e)

    # 给 StorageBackend 中的记录添加 status=distilled 标签（防重）
    if backend and records:
        for record in records:
            uid = record.get("uid", "")
            if uid:
                try:
                    backend.update_tags(uid, add_tags=["status=distilled"])
                except WIKI_BUILDER_OPERATION_ERRORS as e:
                    logger.debug("[WikiBuilder] 更新标签失败 %s: %s", uid, e)


def _log(session_id: str, action: str, detail: str = "") -> None:
    """记录处理日志到 log.md"""
    log_path = _get_wiki_dir() / "log.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"- [{timestamp}] `{session_id[:8]}` {action}: {detail}\n"
    try:
        if log_path.exists():
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
        else:
            # trusted-scan: report owner=hephaestus target=wiki_build_log expires=never not formal knowledge
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("# Wiki Build Log\n\n")
                f.write(line)
    except (OSError, IOError):
        logging.getLogger(__name__).warning("日志写入失败", exc_info=True)


def _link_session_records_to_wiki(records: List[Dict], wiki_page_paths: List[str]) -> None:
    """将 session 的所有 L1 UID 与生成的 Wiki 页面路径建立映射，同时更新 sync_log"""
    from core.config import get_config
    import json

    db_path = get_config().database_dir / "sync_log.db"
    if not db_path.exists() or not records or not wiki_page_paths:
        return
    try:
        uids = [r.get("uid", "") for r in records if r.get("uid")]
        if not uids:
            return
        # 解析 session_id
        session_id = ""
        for r in records:
            for tag in r.get("tags", []):
                if tag.startswith("session="):
                    session_id = tag.split("=", 1)[1]
                    break
            if session_id:
                break

        conn = sqlite3.connect(str(db_path), timeout=10)
        # 1. 写入 l1_wiki_link
        for uid in uids:
            for wpath in wiki_page_paths:
                conn.execute(
                    """INSERT OR IGNORE INTO l1_wiki_link
                       (l1_uid, wiki_page_path, link_type, created_at)
                       VALUES (?, ?, 'wiki_builder', ?)""",
                    (uid, wpath, datetime.now().isoformat()),
                )
        # 2. 更新 sync_log（如果存在该 session 的记录）
        if session_id:
            conn.execute(
                """UPDATE sync_log
                   SET wiki_page_paths = ?, distill_status = 'distilled', distilled_at = ?
                   WHERE session_id = ?""",
                (json.dumps(wiki_page_paths), datetime.now().isoformat(), session_id),
            )
        conn.commit()
        conn.close()
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ):
        logger.warning("l1_wiki_link 记录失败", exc_info=True)


# ========== L1 查询与会话重建 ==========


def fetch_l1_sessions(
    backend: StorageBackend, max_records: int | None = None
) -> Dict[str, List[Dict]]:
    """从 StorageBackend 获取 layer=L1 记录，按 session= 标签分组。

    自动过滤已蒸馏（status=distilled）的记录，避免重复处理。
    """
    logger.info("[WikiBuilder] 查询 StorageBackend 中 L1 记录...")

    try:
        results = backend.list_by_tags(["layer=L1"], limit=max_records)
        all_records = []
        for r in results:
            # 过滤已蒸馏记录
            if "status=distilled" in r.tags:
                continue
            record = {
                "uid": r.uid,
                "content": r.content,
                "tags": r.tags,
                "createTime": r.created_at or "",
                "updateTime": r.updated_at or "",
            }
            all_records.append(record)
    except WIKI_BUILDER_OPERATION_ERRORS as e:
        logger.warning("[WikiBuilder] 查询失败: %s", e, exc_info=True)
        return {}

    sessions: Dict[str, List[Dict]] = {}

    for record in all_records:
        tags = record.get("tags", [])
        session_id = ""
        for tag in tags:
            if tag.startswith("session="):
                session_id = tag.split("=", 1)[1]
                break
        if not session_id:
            continue
        sessions.setdefault(session_id, []).append(record)

    limit_note = f", 本轮上限: {max_records}" if max_records else ""
    print(
        f"[WikiBuilder] L1记录: {len(all_records)}{limit_note}, 待处理 session 数: {len(sessions)}"
    )
    return sessions


def _try_parse_json(content: str) -> Optional[Dict]:
    """尝试解析可能被截断的 JSON（save_session_full 格式兼容）"""
    content = content.strip()
    if not content.startswith("{"):
        return None
    try:
        return json.loads(content)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        logger.warning("[wiki_builder] json.JSONDecodeError suppressed", exc_info=True)
    seg_match = re.search(r'"segment"\s*:\s*"([^"]+)"', content)
    segment = seg_match.group(1) if seg_match else None
    try:
        match = re.search(r'"messages"\s*:\s*(\[[\s\S]*?\])(?:\s*,\s*"|\s*\})', content)
        if match:
            msgs = json.loads(match.group(1))
            return {"_meta": {"segment": segment or "1/1"}, "messages": msgs}
    except (json.JSONDecodeError, ValueError):
        logging.getLogger(__name__).warning("JSON 提取失败", exc_info=True)
    messages = []
    for m in re.finditer(r'"role"\s*:\s*"([^"]*)"', content):
        role = m.group(1)
        pos = m.end()
        content_match = re.search(r'"content"\s*:\s*"([^"]*)"', content[pos : pos + 300])
        if content_match:
            messages.append({"role": role, "content": content_match.group(1)})
    if messages:
        return {"_meta": {"segment": segment or "1/1"}, "messages": messages}
    return None


def _parse_markdown_turns(content: str) -> Optional[List[Dict]]:
    """从 sync_engine 生成的 Markdown 内容中提取消息列表。

    支持两种格式：
    1. 标准格式（build_turn_markdown 生成）：
        ## Turn N
        **User** (model):\n\ncontent\n\n**Assistant**:\n\ncontent\n\n---\n
    2. 简化格式（旧数据/短内容）：
        **User** (model):\n\ncontent\n\n**Assistant**:\n\ncontent\n\n---\n
    3. 分片格式（大内容自动分片生成）：
        [N/M] «title»\n\n...（上述格式之一）

    Returns:
        List[{"role": "user"|"assistant", "content": str}] 或 None
    """
    if not content or not content.strip():
        return None

    # 移除分片前缀 [N/M] «title»
    content = re.sub(r"^\[\d+/\d+\] «[^»]+»\s*\n\n", "", content, count=1)

    # 如果内容以 { 开头，说明是 JSON，不应走 Markdown 解析
    if content.strip().startswith("{"):
        return None

    messages = []

    # 模式1：带 ## Turn N 标题的格式
    # 按 "## Turn" 分割，但保留第一个块（可能在标题前）
    turn_blocks = re.split(r"\n## Turn\s+\d+\s*\n", content)
    if len(turn_blocks) > 1:
        # 有明确的 Turn 标题
        for block in turn_blocks[1:]:  # 跳过第一个空块或前缀
            block_msgs = _extract_messages_from_block(block)
            if block_msgs:
                messages.extend(block_msgs)
        if messages:
            return messages

    # 模式2：无 Turn 标题，整个内容就是一个 turn
    # 尝试从整个内容提取 User/Assistant 对
    block_msgs = _extract_messages_from_block(content)
    if block_msgs:
        return block_msgs

    return None


def _extract_messages_from_block(block: str) -> List[Dict]:
    """从单个 Markdown 块中提取 User/Assistant 消息对。"""
    messages = []  # type: ignore[var-annotated]
    block = block.strip()
    if not block:
        return messages

    # 匹配 **User** (model):\n\ncontent\n\n**Assistant**:\n\ncontent
    # 使用非贪婪匹配，但支持多行内容
    # 分隔符可以是 **Assistant**: 或 **User** 或 --- 或文件末尾
    user_pattern = r"\*\*User\*\*\s*(?:\([^)]+\))?\s*:\s*\n\n(.*?)(?=\n\n\*\*Assistant\*\*|\n\n---|\n\n\*\*User\*\*|\Z)"  # noqa: E501
    assistant_pattern = r"\*\*Assistant\*\*\s*(?:\([^)]+\))?\s*:\s*\n\n(.*?)(?=\n\n\*\*User\*\*|\n\n---|\n\n\*\*Assistant\*\*|\Z)"  # noqa: E501

    # 找到所有 User 和 Assistant 的位置
    user_matches = list(re.finditer(user_pattern, block, re.DOTALL))
    assistant_matches = list(re.finditer(assistant_pattern, block, re.DOTALL))

    # 按在文本中出现的顺序交错合并
    all_matches = []
    for m in user_matches:
        all_matches.append((m.start(), "user", m.group(1).strip()))
    for m in assistant_matches:
        all_matches.append((m.start(), "assistant", m.group(1).strip()))

    all_matches.sort(key=lambda x: x[0])

    for _, role, text in all_matches:
        if text:
            messages.append({"role": role, "content": text})

    return messages


def _extract_capture_artifact_path(content: str) -> Optional[Path]:
    """从 SyncEngine 可见投影中提取完整 capture artifact 路径。"""
    match = re.search(r"Full oversized payload:\s*`([^`]+)`", content)
    if not match:
        return None
    return Path(match.group(1)).expanduser()


def _extract_json_code_block(content: str, heading: str) -> Optional[Dict]:
    pattern = rf"## {re.escape(heading)}\s+````json\s*(.*?)\s*````"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _format_structured_capture_for_distill(structured: Dict) -> str:
    """把 artifact 中的结构化证据投影进蒸馏输入。"""
    lines = []
    for title, key in (
        ("Tool Calls", "tool_calls"),
        ("Tool Results", "tool_results"),
        ("Attachments", "attachments"),
        ("Raw Event Refs", "raw_event_refs"),
        ("Completeness", "completeness"),
    ):
        value = structured.get(key)
        if value:
            lines.extend(
                [
                    f"## {title}",
                    "",
                    "````json",
                    json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str),
                    "````",
                    "",
                ]
            )
    return "\n".join(lines).strip()


def _parse_capture_artifact_content(content: str) -> List[Dict]:
    """Parse already-authorized CaptureService artifact bytes."""
    user_match = re.search(r"## User\s*\n\n(.*?)(?=\n\n---\n\n## Assistant|\Z)", content, re.DOTALL)
    assistant_match = re.search(
        r"## Assistant\s*\n\n(.*?)(?=\n\n---\n\n## Structured Capture|\Z)", content, re.DOTALL
    )
    structured = _extract_json_code_block(content, "Structured Capture") or {}

    messages = []
    if user_match:
        user_text = user_match.group(1).strip()
        if user_text:
            messages.append({"role": "user", "content": user_text})
    if assistant_match:
        assistant_text = assistant_match.group(1).strip()
        structured_text = _format_structured_capture_for_distill(structured)
        if structured_text:
            assistant_text = f"{assistant_text}\n\n{structured_text}".strip()
        if assistant_text:
            messages.append({"role": "assistant", "content": assistant_text})
    return messages


def _read_authorized_capture_artifact(
    record: Dict,
    meta: Dict,
    path: Path,
) -> str | None:
    source_agent = str(meta.get("source") or "")
    session_id = str(meta.get("session_id") or "")
    tags = record.get("tags", [])
    if "capture_truncated=true" not in tags:
        return None
    digest_tags = [
        tag.split("=", 1)[1]
        for tag in tags
        if isinstance(tag, str) and tag.startswith("capture_artifact_sha256=")
    ]
    if len(digest_tags) > 1:
        return None
    turn_number: int | None = None
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("turn="):
            try:
                parsed = int(tag.split("=", 1)[1]) - 1
            except (TypeError, ValueError):
                return None
            if parsed >= 0:
                turn_number = parsed
            break
    if turn_number is None:
        match = re.search(r"^## Turn\s+(\d+)", str(record.get("content") or ""), re.MULTILINE)
        if match:
            turn_number = int(match.group(1)) - 1
    if not source_agent or not session_id or turn_number is None or turn_number < 0:
        return None
    try:
        database_dir = Path(get_config().database_dir)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None
    content = read_managed_capture_artifact_bytes(
        database_dir=database_dir,
        source_agent=source_agent,
        session_id=session_id,
        turn_number=turn_number,
        artifact_type="capture",
        path=path,
    )
    if content is not None:
        if len(digest_tags) != 1:
            return None
        if hashlib.sha256(content).hexdigest() != digest_tags[0]:
            return None
    else:
        content = read_historical_capture_artifact_bytes(
            database_dir=database_dir,
            session_id=session_id,
            turn_number=turn_number,
            artifact_type="capture",
            path=path,
        )
        if (
            content is not None
            and digest_tags
            and hashlib.sha256(content).hexdigest() != digest_tags[0]
        ):
            return None
    if content is None:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeError:
        return None


def _clean_message_content(content: str) -> str:
    """清理消息内容，但保留代码块/工具结果证据。

    这里重建的是蒸馏输入，不应把 SyncEngine 投影出来的 Tool Results JSON
    或错误堆栈直接删掉；统一复用 DistillationEngine 的压缩策略。
    """
    return _clean_for_distill(content)


def _mask_wiki_generated_blocks(content: str) -> str:
    """屏蔽 Wiki 生成的注入块，避免回流

    检测并替换以下标记块：
    - <wiki-context>...</wiki-context>
    - <!-- wiki-injected -->...<!-- /wiki-injected -->
    - <!-- auto-maintained -->...<!-- /auto-maintained -->
    """
    content = re.sub(
        r"<wiki-context>.*?</wiki-context>",
        "[wiki-context-blocked]",
        content,
        flags=re.DOTALL,
    )
    content = re.sub(
        r"<!-- wiki-injected -->.*?<!-- /wiki-injected -->",
        "[wiki-injected-blocked]",
        content,
        flags=re.DOTALL,
    )
    content = re.sub(
        r"<!-- auto-maintained -->.*?<!-- /auto-maintained -->",
        "[auto-maintained-blocked]",
        content,
        flags=re.DOTALL,
    )
    return content


def _record_sort_key(record: Dict) -> int:
    """从 L1 记录中提取排序键（segment / turn / heading）。"""
    content = record.get("content", "")
    tags = record.get("tags", [])

    # 1. JSON _meta.segment (the declared save_session_full record format)
    data = _try_parse_json(content)
    if data and "_meta" in data:
        seg = data["_meta"].get("segment", "1/1")
        try:
            return int(seg.split("/")[0])
        except ValueError:
            logger.warning("[wiki_builder] ValueError suppressed", exc_info=True)

    # 2. 标签中的 segment=N/M（大内容分片格式）
    for tag in tags:
        if tag.startswith("segment="):
            try:
                return int(tag.split("=")[1].split("/")[0])
            except (ValueError, IndexError):
                logger.warning("[wiki_builder] (ValueError, IndexError) suppressed", exc_info=True)

    # 3. 标签中的 turn=N
    for tag in tags:
        if tag.startswith("turn="):
            try:
                return int(tag.split("=")[1])
            except ValueError:
                logger.warning("[wiki_builder] ValueError suppressed", exc_info=True)

    # 4. Markdown 内容中的 ## Turn N
    turn_match = re.search(r"^## Turn\s+(\d+)", content, re.MULTILINE)
    if turn_match:
        return int(turn_match.group(1))

    return 0


def _parse_meta_from_tags(tags: List[str], meta: Dict) -> None:
    """从记录标签中解析 source/model/cwd/session_id/skip-distill 元数据。"""
    for tag in tags:
        if tag.startswith("source=") and not meta["source"]:
            meta["source"] = tag.split("=", 1)[1]
        elif tag.startswith("model=") and not meta["model"]:
            meta["model"] = tag.split("=", 1)[1]
        elif tag.startswith("cwd=") and not meta["cwd"]:
            meta["cwd"] = tag.split("=", 1)[1]
        elif tag.startswith("session=") and not meta["session_id"]:
            meta["session_id"] = tag.split("=", 1)[1]
        if tag == "skip-distill=true":
            meta["has_skip_distill"] = True


def _extract_messages_from_record(record: Dict, meta: Dict) -> List[Dict]:
    """单条 L1 记录解析为消息列表，支持 capture artifact / JSON / Markdown / 纯文本。"""
    content = record.get("content", "")

    artifact_path = _extract_capture_artifact_path(content)
    if artifact_path:
        artifact_content = _read_authorized_capture_artifact(
            record,
            meta,
            artifact_path,
        )
        artifact_messages = (
            _parse_capture_artifact_content(artifact_content)
            if artifact_content is not None
            else []
        )
        if artifact_messages:
            for msg in artifact_messages:
                if isinstance(msg, dict) and "content" in msg:
                    msg["content"] = _clean_message_content(msg["content"])
                    msg["content"] = _mask_wiki_generated_blocks(msg["content"])
            meta.setdefault("artifact_paths", []).append(  # type: ignore[attr-defined]
                str(artifact_path)
            )  # type: ignore[attr-defined]
            meta["used_capture_artifacts"] = True
            return artifact_messages
        meta.setdefault("artifact_rejections", []).append(  # type: ignore[attr-defined]
            "capture_artifact_reference_untrusted"
        )

    data = _try_parse_json(content)
    if data:
        # JSON path for the declared save_session_full record format.
        msgs = data.get("messages", [])
        if isinstance(msgs, list):
            for msg in msgs:
                if isinstance(msg, dict) and "content" in msg:
                    msg["content"] = _clean_message_content(msg["content"])
                    # 回流防护：屏蔽 wiki 生成块
                    msg["content"] = _mask_wiki_generated_blocks(msg["content"])
            return msgs
        return []

    # Markdown 路径（sync_engine 格式，新增）
    md_msgs = _parse_markdown_turns(content)
    if md_msgs:
        for msg in md_msgs:
            if isinstance(msg, dict) and "content" in msg:
                msg["content"] = _clean_message_content(msg["content"])
                msg["content"] = _mask_wiki_generated_blocks(msg["content"])
        return md_msgs

    # Fallback：纯文本，当作完整 system 消息处理；token 分片由下游统一负责。
    cleaned = _clean_message_content(content)
    cleaned = _mask_wiki_generated_blocks(cleaned)
    if cleaned:
        return [
            {
                "role": "system",
                "content": cleaned,
                "timestamp": record.get("createTime", ""),
            }
        ]
    return []


def reconstruct_session(records: List[Dict]) -> Tuple[List[Dict], Dict]:
    """从一个 session 的所有 chunk 中重建完整消息列表"""
    meta = {
        "source": "",
        "model": "",
        "cwd": "",
        "session_id": "",
        "total_chunks": len(records),
        "has_skip_distill": False,
    }

    sorted_records = sorted(records, key=_record_sort_key)
    all_messages: List[Dict] = []
    for record in sorted_records:
        _parse_meta_from_tags(record.get("tags", []), meta)
        all_messages.extend(_extract_messages_from_record(record, meta))

    return all_messages, meta


# ========== 质量评估 ==========


def score_session(messages: List[Dict]) -> Tuple[float, Dict]:
    """对整个会话进行质量评分（使用 DistillScorerV2）。"""
    if not messages:
        return 0.0, {"total_messages": 0, "valid_messages": 0}

    from core.scoring.scorers.distill_scorer_v2 import DistillScorerV2

    scorer = DistillScorerV2()
    session_text = build_session_text(messages)
    card = scorer.score(session_text, dimensions=["distill"])
    distill_score = card.scores.get("distill", 0.0)
    avg_score = distill_score * 100  # Convert to the public 0-100 score scale.
    return avg_score, {
        "total_messages": len(messages),
        "valid_messages": len(messages),
        "avg_score": round(avg_score, 1),
        "scorer": "distill_scorer_v2",
    }


# ========== 主流程 ==========


def _handle_skip_distill(
    session_id: str,
    records: List[Dict],
    messages: List[Dict],
    meta: Dict,
    backend: StorageBackend,
    stats: Dict,
) -> bool:
    """处理 skip-distill 标签的 session，返回是否已跳过。"""
    if not meta.get("has_skip_distill"):
        return False
    _mark_processed(
        session_id,
        meta.get("source", "unknown"),
        len(messages),
        0,
        method="skipped_distill",
        backend=backend,
        records=records,
    )
    _log(session_id, "skip", "skip-distill")
    stats["skipped_distill"] += 1  # type: ignore[operator]
    return True


def _handle_recirculation_block(
    session_id: str,
    records: List[Dict],
    messages: List[Dict],
    meta: Dict,
    backend: StorageBackend,
    recirculation_guard: RecirculationGuard,
    stats: Dict,
) -> bool:
    """处理回流检测命中的 session，返回是否已跳过。"""
    has_recirculation, recirc_detail = recirculation_guard.check_session(messages)
    if not has_recirculation:
        return False
    _mark_processed(
        session_id,
        meta.get("source", "unknown"),
        len(messages),
        0,
        method="recirculation_blocked",
        backend=backend,
        records=records,
    )
    _log(session_id, "skip_recirculation", recirc_detail)
    stats["skipped_recirculation"] += 1  # type: ignore[operator]
    return True


def _handle_low_quality_skip(
    session_id: str,
    records: List[Dict],
    messages: List[Dict],
    meta: Dict,
    avg_score: float,
    backend: StorageBackend,
    stats: Dict,
) -> None:
    """质量评分不足时标记跳过并触发复盘。"""
    _mark_processed(
        session_id,
        meta.get("source", "unknown"),
        len(messages),
        avg_score,
        method="skipped_low_quality",
        backend=backend,
        records=records,
    )
    _log(session_id, "skip_low_quality", f"score:{avg_score:.1f}")
    stats["skipped_low_quality"] += 1  # type: ignore[operator]
    # Phase-1: 复盘触发器 — 低质量跳过触发复盘任务
    try:
        ForcedRetrospective()._create_from_session_end(session_id, "skipped_low_quality")
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ):
        logger.debug("[WikiBuilder] 创建跳过复盘任务失败", exc_info=True)


def _process_distillation_result(
    engine: DistillationEngine,
    session_id: str,
    records: List[Dict],
    messages: List[Dict],
    meta: Dict,
    avg_score: float,
    backend: StorageBackend,
    stats: Dict,
) -> int:
    """执行蒸馏并更新统计，返回创建页面数。"""
    created_pages = 0
    try:
        result = engine.process(session_id, messages, meta)

        if result.judgment in {"knowledge", "skill"} and result.fragments:
            receipt = engine.write_pages_with_receipt(result)
            written = list(receipt.written_pages)
            asset_receipt = getattr(result, "cognition_asset_receipt", None)
            skill_asset_committed = bool(
                result.judgment != "skill"
                or (asset_receipt is not None and asset_receipt.committed)
            )
            if receipt.status == "committed" and skill_asset_committed:
                method = "pipeline_skill" if result.judgment == "skill" else "pipeline"
                for path_str in written:
                    _mark_processed(
                        session_id,
                        meta.get("source", "unknown"),
                        len(messages),
                        avg_score,
                        path_str,
                        method=method,
                        backend=backend,
                        records=records,
                    )
                    created_pages += 1
                    stats["new_knowledge"].append(  # type: ignore[attr-defined]
                        {
                            "session": session_id[:8],
                            "path": path_str,
                            "method": method,
                            "score": avg_score,
                        }
                    )
                # The engine write boundary already published the canonical
                # cognition event; this batch wrapper must not publish again.
                _link_session_records_to_wiki(records, written)
                stats["pipeline_used"] += 1  # type: ignore[operator]
            elif receipt.status == "intentional_skip" and result.judgment == "knowledge":
                _mark_processed(
                    session_id,
                    meta.get("source", "unknown"),
                    len(messages),
                    avg_score,
                    method="pipeline_intentional_skip",
                    backend=backend,
                    records=records,
                )
                stats["processed"] += 1  # type: ignore[operator]
                stats["skip_reasons"].append(  # type: ignore[attr-defined]
                    {
                        "session": session_id[:8],
                        "reason": f"pipeline_intentional_skip: {receipt.terminal_reason}",
                    }
                )
            else:
                # proposal_pending/partial/retryable_failed and every skill
                # without a committed full asset remain eligible for retry.
                stats["failed"] += 1  # type: ignore[operator]
                _log(
                    session_id,
                    "pipeline_nonterminal",
                    f"{receipt.status}:{receipt.terminal_reason}",
                )

            layer_summary = ", ".join(
                f"L{r.layer}({r.name}:{'pass' if r.passed else 'fail'})"
                for r in result.layer_results
            )
            _log(session_id, "pipeline", layer_summary)
        elif result.judgment == "skill":
            # A typed skill judgment without admitted fragments is a retryable
            # contract failure, never a display-only processed state.
            stats["failed"] += 1  # type: ignore[operator]
            _log(session_id, "skill_nonterminal", "skill_judgment_without_fragments")
        else:
            _mark_processed(
                session_id,
                meta.get("source", "unknown"),
                len(messages),
                avg_score,
                method="skipped_by_pipeline",
                backend=backend,
                records=records,
            )
            _log(session_id, "skip_pipeline", result.judgment_reason)
            stats["skipped_low_quality"] += 1  # type: ignore[operator]
            stats["skip_reasons"].append(  # type: ignore[attr-defined]
                {
                    "session": session_id[:8],
                    "reason": f"pipeline_skip: {result.judgment_reason}",
                }
            )
            # Phase-1: 复盘触发器 — 管道跳过触发复盘任务
            try:
                ForcedRetrospective()._create_from_session_end(session_id, "skipped_by_pipeline")
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                sqlite3.Error,
                subprocess.SubprocessError,
            ):
                logger.debug("[WikiBuilder] 创建跳过复盘任务失败", exc_info=True)
    except WIKI_BUILDER_OPERATION_ERRORS as e:
        logger.warning("  [WikiBuilder] 流水线处理失败: %s", e)
        _log(session_id, "error", str(e))
        stats["failed"] += 1  # type: ignore[operator]

    return created_pages


def run_build_cycle(backend: StorageBackend, dry_run: bool = False) -> Dict:
    """执行一轮 L1 → Wiki 批量回追构建。

    本函数是**回追（catch-up）入口**，用于处理未进入 distill_queue 的 L1 记录。
    正常生产路径应通过 HephaestusWorker 消费 distill_queue，而非直接调用本函数。

    Args:
        backend: StorageBackend 实例
        dry_run: 试运行模式
    """
    _ensure_wiki_dirs()
    sessions = fetch_l1_sessions(backend)

    stats = {
        "processed": 0,
        "skipped_low_quality": 0,
        "skipped_incomplete": 0,
        "skipped_similar": 0,
        "skipped_distill": 0,
        "skipped_recirculation": 0,
        "failed": 0,
        "pipeline_used": 0,
        "rule_used": 0,
        "new_knowledge": [],
        "skip_reasons": [],
        "candidates": [],
    }

    # 回流防护
    recirculation_guard = RecirculationGuard()

    # 蒸馏引擎（流水线是唯一支持模式）
    engine = DistillationEngine()

    for session_id, records in sessions.items():
        if len(records) > MAX_SESSION_CHUNKS:
            _log(session_id, "skip", f"too_many_chunks:{len(records)}")
            continue

        if not _is_session_completed(session_id, records):
            stats["skipped_incomplete"] += 1  # type: ignore[operator]
            continue

        if _is_processed(session_id):
            continue

        # 重建会话
        messages, meta = reconstruct_session(records)
        if not messages:
            continue

        # 回流防护：skip-distill 标签
        if _handle_skip_distill(session_id, records, messages, meta, backend, stats):
            continue

        # 回流防护：内容级检测
        if _handle_recirculation_block(
            session_id, records, messages, meta, backend, recirculation_guard, stats
        ):
            continue

        # 质量评分
        avg_score, _score_detail = score_session(messages)

        # 决策：是否蒸馏
        should_distill = avg_score >= 40  # 降门槛，让流水线内部做更精细的判断

        if not should_distill:
            _handle_low_quality_skip(session_id, records, messages, meta, avg_score, backend, stats)
            continue

        if dry_run:
            stats["processed"] += 1  # type: ignore[operator]
            continue

        # ===== 蒸馏（仅流水线模式） =====
        created_pages = _process_distillation_result(
            engine, session_id, records, messages, meta, avg_score, backend, stats
        )
        stats["processed"] += created_pages  # type: ignore[operator]

    # 生成复盘摘要
    _write_retrospective(stats)

    if stats["processed"] > 0:  # type: ignore[operator]
        update_index_md()
        update_moc_pages()
        _git_auto_commit()

    return stats


def _write_retrospective(stats: Dict) -> None:
    """生成并写入本次 build 的复盘摘要"""
    if stats.get("processed", 0) == 0 and not stats.get("skip_reasons"):
        return
    retro_dir = _get_wiki_dir() / "06-Retrospectives"
    retro_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    retro_path = retro_dir / f"retro_{ts}.md"
    lines = [
        f"# 知识蒸馏复盘 — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 新增知识",
    ]
    if stats.get("new_knowledge"):
        for item in stats["new_knowledge"][:20]:
            lines.append(
                f"- `{item['session']}` {item['path']} (score={item['score']:.1f}, method={item['method']})"  # noqa: E501
            )
    else:
        lines.append("- 无")
    lines.extend(["", "## 跳过原因"])
    if stats.get("skip_reasons"):
        for item in stats["skip_reasons"][:20]:
            lines.append(f"- `{item['session']}` {item['reason']}")
    else:
        lines.append("- 无")
    lines.extend(["", "## 待验证（candidate 级别）"])
    if stats.get("candidates"):
        for item in stats["candidates"][:20]:
            lines.append(f"- `{item['session']}` {item['path']} (score={item['score']:.1f})")
    else:
        lines.append("- 无")
    lines.extend(["", "## 可行动提醒"])
    tips = []
    if stats.get("skipped_similar", 0) > 0:
        tips.append(f"- 有 {stats['skipped_similar']} 条因相似度过高被跳过，建议检查是否有重复记录")
    if stats.get("skipped_low_quality", 0) > 0:
        tips.append(f"- 有 {stats['skipped_low_quality']} 条因质量不足被跳过，建议提升对话信息量")
    if stats.get("candidates"):
        tips.append(
            f"- 有 {len(stats['candidates'])} 个 candidate 级别页面待验证，建议人工复核后提升为 main"
        )
    if not tips:
        tips.append("- 本次处理正常，无需特别行动")
    lines.extend(tips)
    lines.extend(
        [
            "",
            "## 复盘口径与后续动作",
            "",
            f"- 本轮成功写入知识页：{int(stats.get('processed', 0) or 0)}",
            f"- 因相似内容跳过：{int(stats.get('skipped_similar', 0) or 0)}",
            f"- 因质量不足跳过：{int(stats.get('skipped_low_quality', 0) or 0)}",
            f"- 待人工验证候选：{len(stats.get('candidates') or [])}",
            "- 判读原则：空列表表示本轮没有对应事项，不代表扫描未执行；若计数与源会话不一致，应回查蒸馏失败记录和 recap task。",
            "- 完成条件：新增页可追溯到来源，会话跳过有明确原因，candidate 经人工确认后再提升为正式知识。",
        ]
    )
    lines.append("")
    try:
        # trusted-scan: report owner=hephaestus target=retrospective_report expires=never generated report
        retro_path.write_text("\n".join(lines), encoding="utf-8")
    except (OSError, IOError):
        logging.getLogger(__name__).warning("复盘文件写入失败", exc_info=True)


def update_index_md():
    """更新 wiki/index.md"""
    index_path = _get_wiki_dir() / "index.md"
    inbox_dir = _get_wiki_dir() / "00-Inbox"

    lines = [
        "# Wiki Index",
        "",
        "本页是 Wiki Builder 维护的会话知识入口，展示最近写入 Inbox 的页面和已处理会话总量。"
        "它只承担导航与运行统计，不替代具体知识页、来源记录或全量 Vault 导航。",
        "",
        "## 使用说明",
        "",
        "- Inbox 最多展示最近 50 个页面；完整页面清单请使用 [[05-MOCs/Mnemos-Navigation/Vault-导航]]。",
        "- 会话总量来自 Wiki Builder 状态库，用于发现写入停滞或统计回退；它不等同于当前文件数。",
        "- 如果 Inbox 计数为零但会话总量非零，应检查页面是否已路由到正式目录，而不是创建占位页补数。",
        "",
    ]

    if inbox_dir.exists():
        md_files: list[tuple[float, Path, str]] = []
        for md_file in inbox_dir.glob("*.md"):
            try:
                content_bytes, metadata = read_native_bytes_with_metadata(md_file)
                md_files.append(
                    (
                        float(metadata.st_mtime),
                        md_file,
                        content_bytes.decode("utf-8"),
                    )
                )
            except (OSError, UnicodeError):
                logging.getLogger(__name__).warning(
                    "Wiki 页面读取失败: %s",
                    md_file,
                    exc_info=True,
                )
        md_files.sort(key=lambda item: item[0], reverse=True)
        lines.append(f"## Inbox ({len(md_files)} sessions)")
        lines.append("")
        for _mtime, md_file, content in md_files[:50]:
            agent = "unknown"
            m = re.search(r"^source_agent:\s*(.+)$", content, re.MULTILINE)
            if m:
                agent = m.group(1).strip()
            name = md_file.stem[:16]
            lines.append(f"- [[{name}]] ({agent})")
        lines.append("")

    try:
        with _get_conn() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM processed_sessions")
            total = cursor.fetchone()[0]
            lines.append("## Stats")
            lines.append(f"- Total sessions: {total}")
            lines.append(f"- Last update: {datetime.now().isoformat()}")
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).warning("索引统计查询失败", exc_info=True)
    # trusted-scan: report owner=hephaestus target=wiki_index expires=never generated index
    index_path.write_text("\n".join(lines), encoding="utf-8")


def update_moc_pages():
    """自动生成 MOC（Map of Content）页面

    生成以下 MOC：
    - 05-MOCs/最近新增.md
    - 05-MOCs/热门知识.md
    - 05-MOCs/待复盘.md
    - 05-MOCs/低置信度待确认.md
    """
    wiki_dir = _get_wiki_dir()
    moc_dir = wiki_dir / "05-MOCs"
    moc_dir.mkdir(parents=True, exist_ok=True)

    # 扫描所有 wiki 页面
    all_pages = [
        _build_page_descriptor(md_file, wiki_dir)
        for md_file in wiki_dir.rglob("*.md")
        if md_file.name != "index.md"
    ]
    all_pages = [p for p in all_pages if p]

    if not all_pages:
        return

    now = time.time()
    seven_days = DURATION_BUCKET_WEEK_DAYS * SEVEN_DAYS

    # 1. 最近新增
    recent = _filter_recent_pages(all_pages, now, seven_days)
    _write_moc(
        moc_dir / "最近新增.md",
        "最近新增",
        "过去 7 天内生成或更新的知识页面。",
        recent,
        lambda p: f"- [[{p['stem']}]] — {p['summary'] or '（无摘要）'} "
        f"<small>覆盖度: {p['coverage'] or '未知'}</small>",
    )

    # 2. 热门知识（优先用 heat_score，fallback 到 mtime）
    hot = _filter_hot_pages(all_pages)
    _write_moc(
        moc_dir / "热门知识.md",
        "热门知识",
        "基于访问频率、搜索命中和反向链接计算的热门页面。",
        hot[:50],
        lambda p: f"- [[{p['stem']}]] — 热力分: {p['heat_score']:.1f} "
        f"<small>{p['summary'] or ''}</small>",
    )

    # 3. 待复盘（pending-verification 或 coverage=partial）
    pending = _filter_pending_pages(all_pages)
    _write_moc(
        moc_dir / "待复盘.md",
        "待复盘",
        "需要人工复核的页面：验证状态为 pending 或内容覆盖不完整。",
        pending,
        lambda p: f"- [[{p['stem']}]] — 置信度: {p['confidence']:.2f}, "
        f"覆盖度: {p['coverage'] or '未知'}",
    )

    # 4. 低置信度待确认
    low_conf = _filter_low_confidence_pages(all_pages)
    _write_moc(
        moc_dir / "低置信度待确认.md",
        "低置信度待确认",
        "置信度低于 0.5 的页面，建议人工确认内容准确性。",
        low_conf,
        lambda p: f"- [[{p['stem']}]] — 置信度: {p['confidence']:.2f} "
        f"<small>{p['summary'] or ''}</small>",
    )


def _write_moc(path: Path, title: str, desc: str, pages: List[Dict], formatter) -> None:
    """写入单个 MOC 页面"""
    lines = [
        "---",
        "type: MOC",
        f"name: {title}",
        f"created_at: {datetime.now().strftime('%Y-%m-%d')}",
        "auto_generated: true",
        "---",
        "",
        f"# {title}",
        "",
        f"> {desc}",
        "",
        f"*本页面由系统自动生成，最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
        "",
    ]
    if pages:
        lines.append(f"共 {len(pages)} 条：")
        lines.append("")
        for p in pages:
            lines.append(formatter(p))
    else:
        lines.append("*当前暂无匹配条目。*")
    lines.extend(
        [
            "",
            "## 使用与维护",
            "",
            f"- 选择口径：{desc}",
            "- 空列表含义：当前没有页面满足上述条件，不代表扫描失败，也不需要创建占位知识页。",
            "- 更新方式：Wiki Builder 重新扫描当前 Vault 元数据后确定性生成本页；目标知识正文不会被本页改写。",
            "- 人工动作：进入具体条目核对来源、覆盖度和置信度；完成后修正目标页元数据，由下一轮生成自动移出清单。",
        ]
    )
    lines.append("")
    try:
        # trusted-scan: report owner=hephaestus target=moc_index expires=never generated index
        path.write_text("\n".join(lines), encoding="utf-8")
    except (OSError, IOError):
        logging.getLogger(__name__).warning("MOC 文件写入失败: %s", path, exc_info=True)


def _git_auto_commit():
    """自动提交 Wiki 变更到 git"""
    try:
        wiki_dir = _get_wiki_dir()
        if not (wiki_dir / ".git").exists():
            return
        result = subprocess.run(
            ["git", "diff", "--quiet"],
            cwd=wiki_dir,
            capture_output=True,
        )
        if result.returncode == 0:
            return
        subprocess.run(["git", "add", "."], cwd=wiki_dir, capture_output=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subprocess.run(
            ["git", "commit", "-m", f"wiki auto-build {timestamp}"],
            cwd=wiki_dir,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        logging.getLogger(__name__).warning("Git 自动提交失败", exc_info=True)


def get_stats() -> Dict:
    """获取处理统计"""
    try:
        with _get_conn() as conn:
            cursor = conn.execute("SELECT COUNT(*), AVG(quality_score) FROM processed_sessions")
            total, avg_score = cursor.fetchone()
            cursor = conn.execute("SELECT COUNT(*) FROM wiki_pages WHERE type = 'source'")
            source_count = cursor.fetchone()[0]
            return {
                "total_processed": total or 0,
                "avg_quality_score": round(avg_score or 0, 1),
                "source_pages": source_count or 0,
                "wiki_dir": str(_get_wiki_dir()),
            }
    except (sqlite3.Error, OSError) as e:
        return {"error": str(e)}


# ========== CLI ==========


def main():
    from core.hephaestus.wiki_builder_cli import (
        WikiBuilderCliDependencies,
        main as cli_main,
    )

    cli_main(
        WikiBuilderCliDependencies(
            create_storage_backend=create_storage_backend,
            get_stats=get_stats,
            operation_errors=WIKI_BUILDER_OPERATION_ERRORS,
            run_build_cycle=run_build_cycle,
        )
    )


if __name__ == "__main__":
    main()
