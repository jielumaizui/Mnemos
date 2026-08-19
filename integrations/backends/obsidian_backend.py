# -*- coding: utf-8 -*-
"""
ObsidianBackend — 本地 Markdown 文件存储后端

职责：
- 将同步内容写入 Obsidian Vault 的 raw/ 目录
- 按时间线物理组织：YYYY/MM/YYYY-MM-DD_HHMM_partNN.md
- YAML frontmatter 标记元数据（session_id, tags, source 等）
- 无内容长度限制（Obsidian 本地文件天然无限制）
- 支持跨天分片：同一天内继续追加，跨天则新建文件

设计原则：
- 物理存储按时间线，项目/标签走逻辑层（Dataview）
- 不主动分片（除非同一天文件超过 800KB 阈值）
- 文件名中的 part_number 仅在同一天内多文件时使用
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, cast

from core.config import ConfigProvider, get_config
from core.vaults.obsidian_registry import (
    ensure_vault_recognized as _ensure_vault_recognized,
    is_vault_registered as _is_vault_registered,
    obsidian_config_path as _core_obsidian_config_path,
)

logger = logging.getLogger(__name__)

OBSIDIAN_OPERATION_ERRORS = (
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.Error,
)

_SK_PREFIX = "sk" + "-"
_GITHUB_TOKEN_PREFIX = "ghp" + "_"

# 敏感信息脱敏规则（应用于写入 raw vault 之前）
_SECRET_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # OpenAI-compatible API key values with long opaque suffixes.
    (re.compile(rf"\b{re.escape(_SK_PREFIX)}[a-zA-Z0-9_\-]{{20,}}"), "[REDACTED_API_KEY]"),
    # Memos PAT / 通用 PAT
    (re.compile(r"\bmemos_pat_[a-zA-Z0-9]{16,}\b", re.IGNORECASE), "[REDACTED_TOKEN]"),
    (re.compile(r"\bpat_[a-zA-Z0-9]{16,}\b", re.IGNORECASE), "[REDACTED_TOKEN]"),
    # GitHub/GitLab 类 PAT.
    (re.compile(rf"\b{re.escape(_GITHUB_TOKEN_PREFIX)}[a-zA-Z0-9]{{36}}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bglpat-[a-zA-Z0-9\-]{20,}\b"), "[REDACTED_TOKEN]"),
    # Bearer token
    (re.compile(r"(Bearer\s+)[a-zA-Z0-9_\-\.]{10,}"), r"\1[REDACTED_TOKEN]"),
    # Authorization header
    (re.compile(r"(Authorization:\s*[A-Za-z]+\s+)[^\s\n\"'`]+"), r"\1[REDACTED_TOKEN]"),
    # 环境变量式 secrets: *_TOKEN="...", *_API_KEY="...", *_SECRET="..."
    (
        re.compile(
            r'(["\']?(?:[A-Z0-9]*_)?(?:API[_-]?KEY|AUTH[_-]?TOKEN|ACCESS[_-]?TOKEN|SECRET|TOKEN)["\']?\s*[:=]\s*)["\'][^"\'\s\n]+["\']',  # noqa: E501
            re.IGNORECASE,
        ),
        r'\1"[REDACTED]"',
    ),
]


def _sanitize_content(content: str) -> str:
    """对即将写入 raw vault 的内容做敏感信息脱敏。"""
    for pattern, repl in _SECRET_PATTERNS:
        content = pattern.sub(repl, content)
    return content


from core.sync_framework.storage_backend import (  # noqa: E402
    StorageBackend,
    StorageResult,
)

# 懒加载 RawIndex，避免循环导入
_RawIndex = None


def _get_raw_index():
    global _RawIndex
    if _RawIndex is None:
        try:
            from core.app.raw_search import RawIndex

            _RawIndex = RawIndex
        except ImportError:
            logger.debug("[ObsidianBackend] RawIndex 未安装，跳过索引更新")
            return None
    return _RawIndex


# 同一天内文件大小阈值（超过则新建 partNN）
_DEFAULT_DAILY_SIZE_THRESHOLD = 800 * 1024  # 800KB


def _parse_tags(tags: List[str]) -> Dict[str, Any]:
    """将 key=value 格式的标签解析为字典"""
    result: Dict[str, Any] = {}
    plain_tags: List[str] = []
    for tag in tags:
        if "=" in tag and not tag.startswith("="):
            key, val = tag.split("=", 1)
            result[key] = val
        else:
            plain_tags.append(tag)
    if plain_tags:
        result["_plain_tags"] = plain_tags
    return result


# frontmatter 标准字段集合（保留顺序，与 _build_frontmatter_dict 保持一致）
_FRONTMATTER_STANDARD_KEYS = {
    "date",
    "session_id",
    "turn",
    "source",
    "content_hash",
    "project",
    "status",
    "content_type",
    "layer",
    "scope",
    "task_id",
    "model",
    "has-code",
    "has-tools",
    "has-reasoning",
    "reasoning_capture",
    "capture_visible",
    "capture_tool_results",
    "capture_reasoning",
    "capture_truncated",
    "capture_loss",
    "skip-distill",
    "_plain_tags",
}

# 这些标准字段需要同时保留在 tags 列表里，以便 list_by_tags(["layer=L1"]) 等查询能命中
_FRONTMATTER_TAG_VISIBLE_STANDARDS = {"layer", "status"}


def _build_frontmatter_dict(
    parsed: Dict[str, Any],
    source: str,
    session_id: str,
    turn_number: int,
    content_hash: str,
    created_at: str,
) -> Dict[str, Any]:
    """从解析后的标签构建 frontmatter 字典。"""
    date_str = parsed.get("date", created_at[:10])
    # time 标签是五维标签中的日期维度（YYYYMMDD），不用于 frontmatter 的 time 字段
    # frontmatter 的 time 始终使用 created_at 的时间部分（HH:MM）
    time_str = created_at[11:16] if len(created_at) > 16 else "00:00"

    fm: Dict[str, Any] = {
        "date": date_str,
        "time": time_str,
        "session_id": session_id,
        "turn": turn_number,
        "source": source,
        "content_hash": content_hash,
        "tags": [],
    }

    # 处理 projects（逗号分隔或多值）
    if "project" in parsed:
        projects = parsed["project"]
        if isinstance(projects, str):
            projects = [p.strip() for p in projects.split(",") if p.strip()]
        fm["projects"] = projects

    tag_list: List[str] = []
    for key, val in parsed.items():
        if key in _FRONTMATTER_STANDARD_KEYS:
            if key not in fm:
                fm[key] = val
            # 部分标准字段（如 layer/status）也需要出现在 tags 中，供按标签查询使用
            if key in _FRONTMATTER_TAG_VISIBLE_STANDARDS:
                tag_list.append(f"{key}={val}")
        else:
            tag_list.append(f"{key}={val}")

    # 纯标签名也加入
    if "_plain_tags" in parsed:
        tag_list.extend(parsed["_plain_tags"])

    # 去重并保持顺序
    seen = set()
    deduped = []
    for t in tag_list:
        if t not in seen:
            seen.add(t)
            deduped.append(t)

    if deduped:
        fm["tags"] = deduped

    return fm


def _serialize_frontmatter(fm: Dict[str, Any]) -> str:
    """将 frontmatter 字典序列化为 YAML 字符串。"""
    lines = ["---"]
    for key, val in fm.items():
        if isinstance(val, list):
            if not val:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                for item in val:
                    lines.append(f"  - {item}")
        elif isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        elif isinstance(val, (int, float)):
            lines.append(f"{key}: {val}")
        else:
            lines.append(f'{key}: "{val}"')
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _make_frontmatter(
    parsed: Dict[str, Any],
    source: str,
    session_id: str,
    turn_number: int,
    content_hash: str,
    created_at: str,
) -> str:
    """生成 YAML frontmatter"""
    fm = _build_frontmatter_dict(
        parsed, source, session_id, turn_number, content_hash, created_at
    )
    return _serialize_frontmatter(fm)


def _split_required_tags(tags: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """将标签查询条件拆分为 key=value 字典与普通标签列表。"""
    required: Dict[str, str] = {}
    required_plain: List[str] = []
    for tag in tags:
        if "=" in tag:
            k, v = tag.split("=", 1)
            required[k] = v
        else:
            required_plain.append(tag)
    return required, required_plain


def _match_required_tags(
    file_tags: List[str],
    file_parsed: Dict[str, Any],
    required: Dict[str, str],
    required_plain: List[str],
) -> bool:
    """判断文件标签是否满足所有查询条件。"""
    for k, v in required.items():
        if file_parsed.get(k) != v and str(file_parsed.get(k)) != v:
            return False
    for pt in required_plain:
        if pt not in file_tags and pt not in file_parsed.get("_plain_tags", []):
            return False
    return True


def _list_by_tags_from_index(
    backend: "ObsidianBackend", tags: List[str], limit: Optional[int]
) -> Optional[List[StorageResult]]:
    """优先使用 RawIndex 查询标签，失败或不可用时返回 None。"""
    try:
        idx = backend._get_raw_index_instance()
        if idx is None:
            return None
        backend._maybe_rebuild_index()
        rows = idx.list_by_tags(tags, limit=limit or 100)
        results: List[StorageResult] = []
        for row in rows:
            results.append(
                StorageResult(
                    uid=row["file_path"],
                    content=row["content"][:500] + "..."
                    if len(row["content"]) > 500
                    else row["content"],
                    tags=row["tags"],
                    metadata={
                        "file_path": str(backend.vault_path / row["file_path"]),
                        "session_id": row["session_id"],
                        "source": row["source"],
                    },
                )
            )
        return results
    except OBSIDIAN_OPERATION_ERRORS as e:
        logger.warning("[ObsidianBackend] RawIndex 标签查询失败，回退扫描: %s", e)
        return None


def _list_by_tags_fallback_scan(
    backend: "ObsidianBackend", tags: List[str], limit: Optional[int]
) -> List[StorageResult]:
    """RawIndex 不可用时回退到本地文件扫描。"""
    results: List[StorageResult] = []
    count = 0
    required, required_plain = _split_required_tags(tags)

    for md_file in sorted(backend.chatlog_dir.rglob("*.md"), reverse=True):
        try:
            text = md_file.read_text(encoding="utf-8")
            file_tags = backend._extract_tags_from_frontmatter(text)
            file_parsed = _parse_tags(file_tags)

            if not _match_required_tags(file_tags, file_parsed, required, required_plain):
                continue

            results.append(
                StorageResult(
                    uid=str(md_file.relative_to(backend.vault_path)),
                    content=text[:500] + "..." if len(text) > 500 else text,
                    tags=file_tags,
                    metadata={"file_path": str(md_file)},
                )
            )
            count += 1
            if limit and count >= limit:
                break
        except OBSIDIAN_OPERATION_ERRORS as e:
            logger.warning("[ObsidianBackend] 标签查询跳过 %s: %s", md_file, e)
    return results


class ObsidianBackend(StorageBackend):
    """Obsidian 本地文件存储后端"""

    def __init__(
        self,
        vault_path: Optional[Path] = None,
        chatlog_subdir: str = "",
        daily_size_threshold: int = _DEFAULT_DAILY_SIZE_THRESHOLD,
        config: Optional[ConfigProvider] = None,
    ):
        cfg = config or get_config()
        self.vault_path = Path(vault_path or cfg.obsidian_vault_path).expanduser()
        self._configured_raw_vault = Path(cfg.obsidian_vault_path).expanduser()
        self._raw_projection_enabled = bool(cfg.get("raw_projection.enabled", True))
        self._scan_cache_ttl = float(cfg.get("storage.obsidian.scan_cache_ttl_seconds", 60))
        self.chatlog_dir = self.vault_path / chatlog_subdir if chatlog_subdir else self.vault_path
        self.chatlog_dir.mkdir(parents=True, exist_ok=True)
        self._raw_index_db_path = self.chatlog_dir / ".raw_index.db"
        self.daily_size_threshold = daily_size_threshold
        # 全库扫描结果缓存（按 chatlog_dir + 查询维度），默认 60 秒 TTL。
        # 使用 OrderedDict 实现 LRU，防止长期运行内存无界增长。
        # 作为实例变量，避免多 vault 共享容量与 TTL 语义；由 RLock 保护并发访问。
        self._scan_cache: OrderedDict[Tuple[str, str, Any], Tuple[float, Any]] = OrderedDict()
        self._scan_cache_max_entries = int(
            cfg.get("storage.obsidian.scan_cache_max_entries", 1024)
        )
        self._scan_cache_lock = threading.RLock()
        # [P1-54] 内存缓存：session_id -> (current_file_path, current_size_bytes)
        # 用于在同一会话中追加内容，减少文件扫描；限制容量避免无界增长
        self._session_file_cache: OrderedDict[str, Tuple[Path, int]] = OrderedDict()
        self._session_file_cache_max_size = 1024
        # 懒加载 RawIndex 实例，用于真正索引查询与增量更新
        self._raw_index: Optional[Any] = None
        # 自动注册到 Obsidian vault 列表（创建 .obsidian + 写入 obsidian.json）
        self._ensure_vault_recognized()

    def _get_raw_index_instance(self) -> Optional[Any]:
        """获取或创建 RawIndex 实例；失败时返回 None 并记录 debug。"""
        if self._raw_index is not None:
            return self._raw_index
        RawIndexCls = _get_raw_index()
        if RawIndexCls is None:
            return None
        try:
            self._raw_index = RawIndexCls(
                raw_dir=self.chatlog_dir,
                db_path=self._raw_index_db_path,
            )
            return self._raw_index
        except OBSIDIAN_OPERATION_ERRORS as e:
            logger.debug("[ObsidianBackend] RawIndex 初始化失败: %s", e)
            return None

    def _close_raw_index(self) -> None:
        """关闭 RawIndex 连接，用于测试隔离。"""
        if self._raw_index is not None:
            try:
                self._raw_index.close()
            except OBSIDIAN_OPERATION_ERRORS as exc:
                logger.debug("关闭 RawIndex 失败，忽略: %s", exc)
            self._raw_index = None

    @property
    def _index_meta_path(self) -> Path:
        return self.chatlog_dir / ".raw_index_meta.json"

    def _current_vault_mtime(self) -> float:
        """返回 chatlog_dir 下所有 .md 文件的最大 mtime。"""
        max_mtime = 0.0
        for md_file in self.chatlog_dir.rglob("*.md"):
            try:
                mtime = md_file.stat().st_mtime
            except OSError:
                continue
            if mtime > max_mtime:
                max_mtime = mtime
        return max_mtime

    def _read_index_meta(self) -> Dict[str, Any]:
        try:
            return cast(Dict[str, Any], json.loads(self._index_meta_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_index_meta(self) -> None:
        meta = {
            "version": 1,
            "last_rebuild_at": datetime.now().isoformat(),
            "vault_mtime": self._current_vault_mtime(),
        }
        try:
            self._index_meta_path.write_text(json.dumps(meta), encoding="utf-8")
        except OSError as exc:
            logger.debug("[ObsidianBackend] 写入索引元数据失败: %s", exc)

    def is_index_stale(self) -> bool:
        """判断当前 RawIndex 是否可能落后于 vault 文件。"""
        meta = self._read_index_meta()
        if not meta:
            return True
        recorded = float(meta.get("vault_mtime", 0.0))
        return self._current_vault_mtime() > recorded

    def rebuild_index(self) -> int:
        """全量重建 RawIndex 并记录 vault mtime。

        Returns:
            索引的文件数量（RawIndex 内部统计）。
        """
        idx = self._get_raw_index_instance()
        if idx is None:
            logger.warning("[ObsidianBackend] RawIndex 不可用，无法重建索引")
            return 0
        try:
            idx.sync_index()
            self._write_index_meta()
            self._invalidate_scan_cache()
            return len(list(self.chatlog_dir.rglob("*.md")))
        except OBSIDIAN_OPERATION_ERRORS as exc:
            logger.warning("[ObsidianBackend] 重建 RawIndex 失败: %s", exc)
            return 0

    def _maybe_rebuild_index(self) -> None:
        """若检测到索引可能过期，则后台重建。"""
        if self.is_index_stale():
            logger.info("[ObsidianBackend] 检测到 RawIndex 可能过期，触发重建")
            self.rebuild_index()

    def _scan_cache_key(self, op: str, extra: Tuple) -> Tuple[str, str, Tuple]:
        return (str(self.chatlog_dir), op, extra)

    def _get_cached_scan(self, op: str, extra: Tuple) -> Optional[Any]:
        key = self._scan_cache_key(op, extra)
        with self._scan_cache_lock:
            entry = self._scan_cache.get(key)
            if entry is None:
                return None
            if time.time() - entry[0] > self._scan_cache_ttl:
                self._scan_cache.pop(key, None)
                return None
            # Move to end so the key survives LRU eviction longer.
            self._scan_cache.move_to_end(key)
            return entry[1]

    def _set_cached_scan(self, op: str, extra: Tuple, result: Any) -> None:
        key = self._scan_cache_key(op, extra)
        with self._scan_cache_lock:
            self._scan_cache[key] = (time.time(), result)
            self._scan_cache.move_to_end(key)
            while len(self._scan_cache) > self._scan_cache_max_entries:
                self._scan_cache.popitem(last=False)

    def _invalidate_scan_cache(self) -> None:
        prefix = str(self.chatlog_dir)
        with self._scan_cache_lock:
            for key in list(self._scan_cache.keys()):
                if key[0] == prefix:
                    del self._scan_cache[key]

    # ---------- StorageBackend 接口实现 ----------

    def _raw_projection_owns_target(self) -> bool:
        """Return True when this backend targets the canonical raw vault projection."""
        if not self._raw_projection_enabled:
            return False
        try:
            chatlog_dir = self.chatlog_dir.resolve()
            raw_vault = self._configured_raw_vault.resolve()
            return chatlog_dir == raw_vault or chatlog_dir.is_relative_to(raw_vault)
        except OSError:
            return False

    def save(
        self,
        content: str,
        tags: List[str],
        title: str,
        *,
        now: Optional[datetime] = None,
    ) -> List[StorageResult]:
        """
        保存内容到 Obsidian。

        策略：
        1. 解析 tags 提取 session_id 和 turn 信息
        2. 决定写入哪个文件（同 session 同天追加，跨天或超大小则新建）
        3. 写入 frontmatter + 内容
        4. 返回 StorageResult（uid = 文件路径）

        Args:
            content: 待保存的 Markdown 内容。
            tags: key=value 格式的标签列表。
            title: 标题（当前未写入文件名，仅用于生成 StorageResult）。
            now: 可选指定时间戳；未提供时使用当前时间。历史回填/迁移时可复用原始时间。
        """
        if self._raw_projection_owns_target():
            logger.warning(
                "[ObsidianBackend] raw_projection 已接管 raw vault，拒绝 legacy 直写: %s",
                self.chatlog_dir,
            )
            return []

        parsed = _parse_tags(tags)
        source = parsed.get("source", "unknown")
        session_id = parsed.get("session", "")
        turn_str = parsed.get("turn", "0")
        try:
            turn_number = int(turn_str)
        except ValueError:
            turn_number = 0
        # 写入前脱敏，避免 API key / token 等 secrets 进入 raw vault
        content = _sanitize_content(content)

        content_hash = parsed.get("content_hash", "")
        if not content_hash:
            content_hash = hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]

        now = now or datetime.now()
        file_path = self._resolve_file_path(session_id, now)

        # 构建完整内容块
        created_at = now.isoformat()
        frontmatter = _make_frontmatter(
            parsed, source, session_id, turn_number, content_hash, created_at
        )
        block = frontmatter + content + "\n\n---\n\n"

        # 追加写入
        is_new = not file_path.exists()
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(block)

        # 更新缓存
        new_size = file_path.stat().st_size
        self._session_file_cache[session_id] = (file_path, new_size)

        # 新增内容可能改变 list_by_tags / search 结果，失效扫描缓存
        self._invalidate_scan_cache()

        logger.info(
            "[ObsidianBackend] %s %s (session=%s..., turn=%s, size=%sB)",
            "新建" if is_new else "追加",
            file_path.name,
            session_id[:8],
            turn_number,
            new_size,
        )

        # 触发 RawIndex 增量索引更新（后台，不阻塞写入）
        try:
            idx = self._get_raw_index_instance()
            if idx is not None:
                idx.index_file(file_path)
        except OBSIDIAN_OPERATION_ERRORS as e:
            logger.debug("[ObsidianBackend] RawIndex 增量更新跳过: %s", e)

        return [
            StorageResult(
                uid=str(file_path.relative_to(self.vault_path)),
                content=content[:200] + "..." if len(content) > 200 else content,
                tags=tags,
                metadata={
                    "file_path": str(file_path),
                    "is_new": is_new,
                    "file_size": new_size,
                    "session_id": session_id,
                    "turn_number": turn_number,
                },
                created_at=created_at,
            )
        ]

    def search(self, query: str, limit: Optional[int] = None) -> List[StorageResult]:
        """
        全文搜索 — 优先使用 RawIndex FTS，失败时回退到本地文件扫描。
        """
        cache_extra = (query.lower(), limit)
        cached = self._get_cached_scan("search", cache_extra)
        if cached is not None:
            return cached  # type: ignore[no-any-return]

        results: List[StorageResult] = []

        # 优先走真正索引
        try:
            idx = self._get_raw_index_instance()
            if idx is not None:
                self._maybe_rebuild_index()
                raw_results = idx.search(query, limit=limit or 50)
                for r in raw_results:
                    file_path = self.vault_path / r.file_path
                    try:
                        text = (
                            file_path.read_text(encoding="utf-8")
                            if file_path.exists()
                            else r.snippet
                        )
                    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                        text = r.snippet
                    results.append(
                        StorageResult(
                            uid=r.file_path,
                            content=text[:500] + "..." if len(text) > 500 else text,
                            tags=r.tags,
                            metadata={
                                "file_path": str(file_path),
                                "session_id": r.session_id,
                                "source": r.source,
                            },
                        )
                    )
                self._set_cached_scan("search", cache_extra, results)
                return results
        except OBSIDIAN_OPERATION_ERRORS as e:
            logger.warning("[ObsidianBackend] RawIndex 搜索失败，回退扫描: %s", e)

        # Fallback: 本地文件扫描
        count = 0
        for md_file in sorted(self.chatlog_dir.rglob("*.md"), reverse=True):
            try:
                text = md_file.read_text(encoding="utf-8")
                if query.lower() in text.lower():
                    tags = self._extract_tags_from_frontmatter(text)
                    results.append(
                        StorageResult(
                            uid=str(md_file.relative_to(self.vault_path)),
                            content=text[:500] + "..." if len(text) > 500 else text,
                            tags=tags,
                            metadata={"file_path": str(md_file)},
                        )
                    )
                    count += 1
                    if limit and count >= limit:
                        break
            except OBSIDIAN_OPERATION_ERRORS as e:
                logger.warning("[ObsidianBackend] 搜索跳过 %s: %s", md_file, e, exc_info=True)
        self._set_cached_scan("search", cache_extra, results)
        return results

    def list_by_tags(self, tags: List[str], limit: Optional[int] = None) -> List[StorageResult]:
        """
        按标签查询 — 优先使用 RawIndex 标签表，失败时回退到文件扫描。
        """
        cache_extra = (tuple(sorted(tags)), limit)
        cached = self._get_cached_scan("list_by_tags", cache_extra)
        if cached is not None:
            return cached  # type: ignore[no-any-return]

        results = _list_by_tags_from_index(self, tags, limit)
        if results is not None:
            self._set_cached_scan("list_by_tags", cache_extra, results)
            return results

        results = _list_by_tags_fallback_scan(self, tags, limit)
        self._set_cached_scan("list_by_tags", cache_extra, results)
        return results

    def get_by_id(self, uid: str) -> Optional[StorageResult]:
        """按相对路径获取文件内容"""
        file_path = self.vault_path / uid
        if not file_path.exists():
            return None
        try:
            text = file_path.read_text(encoding="utf-8")
            tags = self._extract_tags_from_frontmatter(text)
            return StorageResult(
                uid=uid,
                content=text,
                tags=tags,
                metadata={"file_path": str(file_path)},
            )
        except OBSIDIAN_OPERATION_ERRORS as e:
            logger.warning("[ObsidianBackend] 读取失败 %s: %s", file_path, e, exc_info=True)
            return None

    def update_tags(
        self,
        uid: str,
        add_tags: Optional[List[str]] = None,
        remove_tags: Optional[List[str]] = None,
    ) -> Optional[StorageResult]:
        """
        增量更新文件 frontmatter 中的 tags。

        Obsidian 文件中同 session 的 turn 追加在同一个文件里，
        每个 turn 有自己独立的 frontmatter 块。本方法会更新文件中
        所有 frontmatter 块的 tags 字段。
        """
        if not add_tags and not remove_tags:
            return self.get_by_id(uid)

        file_path = self.vault_path / uid
        if not file_path.exists():
            return None

        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, IOError) as e:
            logger.warning("[ObsidianBackend] 读取失败 %s: %s", file_path, e, exc_info=True)
            return None

        # 正则分割：找到所有 frontmatter 块（必须包含 session_id，避免正文水平线被误切）
        parts = re.split(
            r"(---\n(?=.*?\nsession_id:).*?\n---\n|\n---\n(?=.*?\nsession_id:).*?\n---\n)",
            text,
            flags=re.DOTALL,
        )
        modified = False

        for i, part in enumerate(parts):
            if not part.startswith("\n---\n") and not part.startswith("---\n"):
                continue

            # 提取 frontmatter 内容（去掉 --- 包裹）
            fm_match = re.match(r"\n?---\n(.*?)\n---\n", part, re.DOTALL)
            if not fm_match:
                continue

            fm_body = fm_match.group(1)

            # 解析当前 tags
            current_tags = self._extract_tags_from_frontmatter("---\n" + fm_body + "\n---\n")
            new_tags = set(current_tags)
            if add_tags:
                new_tags.update(add_tags)
            if remove_tags:
                new_tags -= set(remove_tags)

            if new_tags == set(current_tags):
                continue

            # 重建 frontmatter body：替换 tags 字段
            new_fm_body = self._replace_tags_in_frontmatter(fm_body, sorted(new_tags))
            _ = part[: fm_match.start()] + "\n---\n" + new_fm_body + "\n---\n"
            # fm_match.start() 是相对于 part 的起始位置
            # 但 part 本身就是以 \n---\n 或 ---\n 开头的，所以 fm_match.start() 应该是 0
            # 直接用替换
            parts[i] = (
                "---\n" + new_fm_body + "\n---\n"
                if part.startswith("---\n")
                else "\n---\n" + new_fm_body + "\n---\n"
            )
            modified = True

        if not modified:
            return self.get_by_id(uid)

        new_text = "".join(parts)
        try:
            file_path.write_text(new_text, encoding="utf-8")
        except (OSError, IOError) as e:
            logger.warning("[ObsidianBackend] 写入失败 %s: %s", file_path, e, exc_info=True)
            return None

        # 标签已变更，失效当前 scope 的扫描缓存，避免 list_by_tags/search 返回旧结果。
        self._invalidate_scan_cache()

        # 同步更新 RawIndex 中单文件索引
        try:
            idx = self._get_raw_index_instance()
            if idx is not None:
                idx.index_file(file_path)
        except OBSIDIAN_OPERATION_ERRORS as e:
            logger.debug("[ObsidianBackend] RawIndex 标签更新跳过: %s", e)

        logger.info(
            "[ObsidianBackend] 更新标签 %s: +%s -%s", uid, add_tags or [], remove_tags or []
        )
        return self.get_by_id(uid)

    @staticmethod
    def _replace_tags_in_frontmatter(fm_body: str, new_tags: List[str]) -> str:
        """替换 frontmatter body 中的 tags 字段，保持其他字段不变。"""
        lines = fm_body.split("\n")
        result = []
        in_tags = False
        tags_replaced = False

        for line in lines:
            if line.startswith("tags:"):
                in_tags = True
                tags_replaced = True
                if not new_tags:
                    result.append("tags: []")
                else:
                    result.append("tags:")
                    for tag in new_tags:
                        result.append(f"  - {tag}")
                continue
            if in_tags:
                # 跳过旧的 tags 行
                if line.startswith("  - ") or line.startswith("- "):
                    continue
                in_tags = False
            result.append(line)

        # 如果原来没有 tags 字段，在文件末尾添加
        if not tags_replaced and new_tags:
            result.append("tags:")
            for tag in new_tags:
                result.append(f"  - {tag}")

        return "\n".join(result)

    def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        ok = self.chatlog_dir.exists() and self.chatlog_dir.is_dir()
        return {
            "status": "ok" if ok else "error",
            "message": (
                f"chatlog_dir={self.chatlog_dir}" if ok else f"目录不存在: {self.chatlog_dir}"
            ),
            "vault_path": str(self.vault_path),
            "daily_size_threshold": self.daily_size_threshold,
        }

    # ---------- 内部方法 ----------

    def _resolve_file_path(self, session_id: str, now: datetime) -> Path:
        """
        确定内容应写入哪个文件。

        规则：
        1. 同 session + 同天 → 复用当天文件（检查大小是否超过阈值）
        2. 同 session + 跨天 → 新建文件
        3. 文件超过 daily_size_threshold → 新建 partNN

        路径结构：YYYY/MM/DD/YYYY-MM-DD_HHMM_partNN.md

        为减少每天扫描并读取所有文件的开销，使用同目录下的
        `.mnemos_session_index.json` 缓存 session → 文件名列表的映射。
        """
        # 年/月/日 三级目录
        day_dir = self.chatlog_dir / f"{now.year}" / f"{now.month:02d}" / f"{now.day:02d}"
        day_dir.mkdir(parents=True, exist_ok=True)

        date_prefix = now.strftime("%Y-%m-%d")
        index = self._load_session_index(day_dir)

        # 检查缓存
        cached = self._session_file_cache.get(session_id)
        if cached:
            cached_path, cached_size = cached
            # 缓存命中：检查是否在同一天的目录下且未超大小
            if (
                cached_path.parent == day_dir
                and cached_path.name.startswith(date_prefix)
                and cached_size < self.daily_size_threshold
            ):
                # 命中后移到队尾，保持最近使用
                self._session_file_cache.move_to_end(session_id)
                return cached_path

        # 优先从索引查找该 session 的最新文件
        session_files = self._session_files_from_index(day_dir, session_id, index)
        if session_files:
            latest = session_files[-1]
            size = latest.stat().st_size
            if size < self.daily_size_threshold:
                self._set_session_file_cache(session_id, latest, size)
                return latest
            # 超大小，新建 part
            part_num = self._extract_part_number(latest.name) + 1
        else:
            # Index miss: rebuild the current session mapping from same-day files.
            pattern = f"{date_prefix}_*_part*.md"
            existing = sorted(day_dir.glob(pattern))
            session_files = []
            for f in existing:
                try:
                    text = f.read_text(encoding="utf-8")
                    if f'session_id: "{session_id}"' in text or f"session_id: {session_id}" in text:
                        session_files.append(f)
                except (OSError, IOError):
                    pass

            if session_files:
                latest = session_files[-1]
                size = latest.stat().st_size
                # 回写索引，避免下次再扫描
                index[session_id] = [f.name for f in session_files]
                self._save_session_index(day_dir, index)
                if size < self.daily_size_threshold:
                    self._set_session_file_cache(session_id, latest, size)
                    return latest
                part_num = self._extract_part_number(latest.name) + 1
            else:
                part_num = 1

        # 生成新文件名，并确保不会覆盖其他 session 的现有文件
        time_str = now.strftime("%H%M")
        candidate_part = part_num
        while True:
            filename = f"{date_prefix}_{time_str}_part{candidate_part:02d}.md"
            new_path = day_dir / filename
            if not new_path.exists():
                break
            # 文件名已被占用，向上递增 part 编号
            candidate_part += 1

        # 更新索引
        names = set(index.get(session_id, []))
        names.add(filename)
        index[session_id] = sorted(names)
        self._save_session_index(day_dir, index)
        return new_path

    def _session_index_path(self, day_dir: Path) -> Path:
        return day_dir / ".mnemos_session_index.json"

    def _load_session_index(self, day_dir: Path) -> Dict[str, List[str]]:
        """读取 session → 文件名列表 的本地索引。"""
        idx_path = self._session_index_path(day_dir)
        if not idx_path.exists():
            return {}
        try:
            data = json.loads(idx_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): list(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("[ObsidianBackend] 读取 session index 失败", exc_info=True)
        return {}

    def _save_session_index(self, day_dir: Path, index: Dict[str, List[str]]) -> None:
        """保存 session → 文件名列表 的本地索引。"""
        idx_path = self._session_index_path(day_dir)
        try:
            idx_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("[ObsidianBackend] 写入 session index 失败", exc_info=True)

    def _session_files_from_index(
        self,
        day_dir: Path,
        session_id: str,
        index: Dict[str, List[str]],
    ) -> List[Path]:
        """从索引中获取某 session 当天存在的文件列表（按文件名排序）。"""
        names = [n for n in index.get(session_id, []) if (day_dir / n).exists()]
        return sorted(day_dir / n for n in names)

    def _set_session_file_cache(self, session_id: str, path: Path, size: int) -> None:
        """有界写入 session 文件缓存，超出容量时淘汰最久未使用项。"""
        self._session_file_cache[session_id] = (path, size)
        self._session_file_cache.move_to_end(session_id)
        while len(self._session_file_cache) > self._session_file_cache_max_size:
            self._session_file_cache.popitem(last=False)

    @staticmethod
    def _extract_part_number(filename: str) -> int:
        """从文件名提取 part 序号"""
        match = re.search(r"_part(\d+)", filename)
        return int(match.group(1)) if match else 1

    @staticmethod
    def _extract_tags_from_frontmatter(text: str) -> List[str]:
        """从 frontmatter 提取 tags 列表"""
        if not text.startswith("---"):
            return []
        end = text.find("\n---", 3)
        if end == -1:
            return []
        fm_text = text[3:end].strip()
        tags: List[str] = []
        in_tags = False
        for line in fm_text.split("\n"):
            line = line.rstrip()
            if line.startswith("tags:"):
                in_tags = True
                val = line[len("tags:") :].strip()
                if val and val != "[]":
                    # inline list: tags: [a, b]
                    if val.startswith("[") and val.endswith("]"):
                        inner = val[1:-1]
                        tags.extend(
                            [t.strip().strip('"').strip("'") for t in inner.split(",") if t.strip()]
                        )
                continue
            if in_tags:
                if line.startswith("  - "):
                    tags.append(line[4:].strip().strip('"').strip("'"))
                elif line.startswith("- "):
                    tags.append(line[2:].strip().strip('"').strip("'"))
                else:
                    in_tags = False
        return tags

    def _ensure_vault_recognized(self):
        """实例方法包装：确保当前 vault 被 Obsidian 识别。"""
        ensure_vault_recognized(self.vault_path)


def ensure_vault_recognized(vault_path: Path) -> bool:
    """Compatibility wrapper for the core Obsidian registry port."""
    return _ensure_vault_recognized(vault_path)


def is_vault_registered(vault_path: Path) -> bool:
    """Compatibility wrapper for the core Obsidian registry port."""
    return _is_vault_registered(vault_path)


def _obsidian_config_path() -> Optional[Path]:
    """Compatibility wrapper for the core Obsidian registry port."""
    return _core_obsidian_config_path()
