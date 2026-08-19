"""
数据源读取器

统一读取 Layer 1 (raw) 和 Layer 2 (wiki) 的内容，
输出标准化的 SourceItem，供 Observation Engine 分析。

**关键设计：内容分层与来源标记**

不是所有 wiki 内容都能代表用户认知。必须区分：

1. User-Generated（用户生成）→ 进入 L3
   - 用户与 AI 的对话蒸馏
   - 用户自己写的笔记、总结、复盘
   - 用户做的决策、判断、评论

2. System-Generated（系统生成）→ 排除
   - 系统提醒、复盘模板、自动报告
   - 这些内容的词汇是系统写的，不代表用户

3. External-Quoted（外部引用）→ 行为信号保留，认知内容排除
   - 书籍摘录、新闻、文献、公司宣贯
   - 外部文本本身不反映用户认知
   - 但"用户选择读什么"是 Attention 信号

4. Likely-Pasted（疑似复制粘贴）→ 标记但不排除
   - 基于字数分布 P99 检测
   - 用户选择粘贴这段内容 → 行为信号
   - 内容本身降级置信度处理
"""

import itertools
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


import yaml

from core.frontmatter import fm_get
from core.sync_framework.raw_event_store import (
    CanonicalRawReadError,
    CanonicalRawTurn,
    count_current_raw_turns_readonly,
    list_current_raw_turns_readonly,
)

logger = logging.getLogger(__name__)

_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _is_sha256(value: str) -> bool:
    return bool(_SHA256_PATTERN.fullmatch(str(value or "")))


class ContentTier(str, Enum):
    """内容分层"""

    USER_GENERATED = "user_generated"  # 用户生成 → 进入 L3
    SYSTEM_GENERATED = "system_generated"  # 系统生成 → 排除
    EXTERNAL_QUOTED = "external_quoted"  # 外部引用 → 行为信号保留
    LIKELY_PASTED = "likely_pasted"  # 疑似复制粘贴 → 标记但不排除
    UNKNOWN = "unknown"  # 未知 → 默认进入 L3


class ContentSource(str, Enum):
    """内容来源：谁产生了这条内容的原始文本"""

    NATIVE_DIALOGUE = "native_dialogue"  # 原生对话（用户或AI正常输入）
    LIKELY_PASTED = "likely_pasted"  # 用户复制粘贴的外部内容
    EXTERNAL_FILE = "external_file"  # 外部文件导入（PDF/PPT/书籍）
    USER_NOTE = "user_note"  # 用户自己写的笔记/日记
    UNKNOWN = "unknown"  # 无法判断


class UserIntent(str, Enum):
    """用户意图：用户为什么要引入这段内容"""

    SEEKING_JUDGMENT = "seeking_judgment"  # 请AI判断/评估
    SEEKING_SUMMARY = "seeking_summary"  # 请AI总结/提炼
    EXPRESSING_AGREEMENT = "expressing_agreement"  # 表达认同/共鸣
    EXPRESSING_DOUBT = "expressing_doubt"  # 表达质疑/反对
    SHARING_INFORMATION = "sharing_information"  # 单纯分享信息
    ASKING_QUESTION = "asking_question"  # 基于这段内容提问
    CURATE_OR_DECISION_MATERIAL = "curate_or_decision_material"  # 主动提供材料供整理/决策
    UNKNOWN = "unknown"  # 无法判断


class CanonicalRawUnavailable(RuntimeError):
    """Raised when a canonical Raw-backed reader cannot prove its input."""


def _scalar_frontmatter_text(value: Any) -> Optional[str]:
    """Return a scalar metadata value; lists/dicts are not source identities."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


@dataclass
class SourceItem:
    """
    标准化的来源内容项
    """

    source_type: str  # "raw" | "wiki"
    file_path: str  # 原始文件路径
    content: str  # 正文内容（markdown）
    frontmatter: Dict = field(default_factory=dict)
    content_tier: ContentTier = ContentTier.UNKNOWN
    content_source: ContentSource = ContentSource.UNKNOWN
    user_intent: UserIntent = UserIntent.UNKNOWN

    # 从 frontmatter 提取的关键字段
    timestamp: Optional[datetime] = None
    session_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    source_agent: Optional[str] = None  # claude / kimi / codex 等
    content_type: Optional[str] = None

    # Canonical Raw provenance.  These fields are populated only by the
    # read-only raw_events.db reader (or a hash-verified v2 projection
    # verifier), never inferred from a directory name.
    raw_event_id: str = ""
    raw_revision_id: str = ""
    raw_content_hash: str = ""
    user_content: str = ""
    assistant_content: str = ""
    source_stream: str = ""
    cursor_token: Dict[str, str] = field(default_factory=dict)

    # Calibration lineage is intentionally smaller than the visible source
    # payload.  A derived Wiki page carries the exact immutable Raw revisions
    # and spans recorded in its frontmatter; a canonical Raw item names itself.
    # Missing or malformed provenance never becomes an "independent" source.
    lineage_revision_ids: tuple[str, ...] = ()
    lineage_root_hashes: tuple[tuple[str, str], ...] = ()
    source_span_ids: tuple[str, ...] = ()
    lineage_status: str = ""
    source_content_hash: str = ""

    def __post_init__(self):
        fm = self.frontmatter
        self.session_id = _scalar_frontmatter_text(
            fm.get("session_id")
        ) or _scalar_frontmatter_text(self.session_id)
        self.source_agent = _scalar_frontmatter_text(
            fm.get("source") or fm.get("model") or fm.get("来源")
        ) or _scalar_frontmatter_text(self.source_agent)
        self.content_type = _scalar_frontmatter_text(
            fm.get("content_type") or fm.get("layer") or fm.get("type") or fm.get("类型")
        ) or _scalar_frontmatter_text(self.content_type)
        raw_tags = fm.get("tags", fm.get("_plain_tags", []))
        if isinstance(raw_tags, (list, tuple)):
            self.tags = [
                tag
                for value in raw_tags
                if (tag := _scalar_frontmatter_text(value)) is not None
            ]
        elif (tag := _scalar_frontmatter_text(raw_tags)) is not None:
            self.tags = [tag]
        else:
            self.tags = []

        if not self.source_content_hash:
            self.source_content_hash = "sha256:" + hashlib.sha256(
                str(self.content or "").encode("utf-8")
            ).hexdigest()
        self._bind_calibration_lineage()

        # 解析时间
        date_str = fm.get("date") or fm.get("created_at") or fm.get("创建日期")
        if date_str:
            try:
                if isinstance(date_str, str):
                    value = date_str.strip().replace("Z", "+00:00")
                    try:
                        parsed = datetime.fromisoformat(value)
                    except ValueError:
                        parsed = None
                    if parsed is None:
                        for fmt in (
                            "%Y-%m-%d",
                            "%Y-%m-%dT%H:%M:%S",
                            "%Y-%m-%dT%H:%M:%S.%f",
                        ):
                            try:
                                parsed = datetime.strptime(value, fmt)
                                break
                            except ValueError:
                                continue
                    if parsed is not None:
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        else:
                            parsed = parsed.astimezone(timezone.utc)
                        self.timestamp = parsed
                elif isinstance(date_str, datetime):
                    self.timestamp = (
                        date_str.replace(tzinfo=timezone.utc)
                        if date_str.tzinfo is None
                        else date_str.astimezone(timezone.utc)
                    )
                elif isinstance(date_str, date):
                    self.timestamp = datetime.combine(
                        date_str,
                        datetime.min.time(),
                        tzinfo=timezone.utc,
                    )
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logger.debug("解析 frontmatter 日期失败: %s", date_str, exc_info=True)

    @staticmethod
    def _span_id(revision_id: str, span_start: int, span_end: int) -> str:
        return f"raw-span:{revision_id}:{span_start}:{span_end}"

    def _bind_calibration_lineage(self) -> None:
        """Bind exact Raw roots without inferring identity from a file path."""

        if self.raw_revision_id:
            span_end = len(str(self.content or ""))
            root_hash = str(self.raw_content_hash or self.source_content_hash)
            if not _is_sha256(root_hash):
                self.lineage_revision_ids = ()
                self.lineage_root_hashes = ()
                self.source_span_ids = ()
                self.lineage_status = "malformed"
                return
            self.lineage_revision_ids = (str(self.raw_revision_id),)
            self.lineage_root_hashes = (
                (
                    str(self.raw_revision_id),
                    root_hash,
                ),
            )
            self.source_span_ids = (
                (self._span_id(str(self.raw_revision_id), 0, span_end),)
                if span_end > 0
                else ()
            )
            self.lineage_status = (
                "canonical_raw" if span_end > 0 else "canonical_raw_no_visible_span"
            )
            return

        raw_refs = self.frontmatter.get("raw_event_refs")
        if raw_refs is None:
            self.lineage_status = "unprovenanced"
            return
        if not isinstance(raw_refs, list):
            self.lineage_status = "malformed"
            return

        revisions: list[str] = []
        root_hashes: list[tuple[str, str]] = []
        spans: list[str] = []
        try:
            for raw_ref in raw_refs:
                if not isinstance(raw_ref, dict):
                    raise ValueError("raw lineage ref must be an object")
                revision_id = str(raw_ref.get("revision_id") or "").strip()
                content_hash = str(raw_ref.get("content_hash") or "").strip()
                raw_span_start = raw_ref.get("span_start")
                raw_span_end = raw_ref.get("span_end")
                if raw_span_start is None or raw_span_end is None:
                    raise ValueError("raw lineage ref span is missing")
                span_start = int(raw_span_start)
                span_end = int(raw_span_end)
                if (
                    not revision_id
                    or not _is_sha256(content_hash)
                    or span_start < 0
                    or span_end <= span_start
                ):
                    raise ValueError("raw lineage ref is incomplete")
                if revision_id not in revisions:
                    revisions.append(revision_id)
                identity = (revision_id, content_hash)
                if identity not in root_hashes:
                    root_hashes.append(identity)
                span_id = self._span_id(revision_id, span_start, span_end)
                if span_id not in spans:
                    spans.append(span_id)
        except (TypeError, ValueError):
            self.lineage_revision_ids = ()
            self.lineage_root_hashes = ()
            self.source_span_ids = ()
            self.lineage_status = "malformed"
            return

        if not revisions or not spans:
            self.lineage_status = "malformed"
            return
        self.lineage_revision_ids = tuple(revisions)
        self.lineage_root_hashes = tuple(root_hashes)
        self.source_span_ids = tuple(spans)
        self.lineage_status = "derived_exact"


class SourceReader:
    """
    数据源读取器，带内容分层过滤

    排除规则（SYSTEM_GENERATED）：
    - 08-Reminders/ 目录下的系统提醒
    - frontmatter 标记 source: system
    - frontmatter 标记 auto_updated: true + mnemos_type: dashboard
    - 99-Reports/ 下的系统自动报告

    外部引用规则（EXTERNAL_QUOTED）：
    - 07-Shadow/ 目录下的影子页面
    - frontmatter 明确标记来源为外部（书籍、新闻、文献）

    复制粘贴检测（LIKELY_PASTED）：
    - 基于同 session 用户消息长度的 P99 基线
    - 超过 P99 且 > 300 字 → 标记为 likely_pasted
    """

    # 系统生成目录（排除）
    # 包括自动提醒、报告、聊天记录、数据集、重建报告，以及认知层自动投影
    # L2.4-KG / L3-Observations / L4-Reflections / L5-Feedback 都是系统生成的只读投影，
    # 如果作为输入回读，会造成自我放大和重复计数。
    SYSTEM_DIRS = {
        "08-Reminders",
        "99-Reports",
        "08-Chatlogs",
        "08-Datasets",
        ".rebuild-reports",
        "L2.4-KG",
        "L3-Observations",
        "L4-Reflections",
        "L5-Feedback",
    }

    # 外部引用目录（行为信号保留）
    EXTERNAL_DIRS = {"07-Shadow"}

    # Canonical Raw is lossless and can contain very large individual
    # revisions.  Bound an ordinary page by both item count and compressed
    # snapshot bytes; the Raw iterator still returns one oversized revision
    # alone so cursor progress never turns into content loss.
    MAX_CANONICAL_RAW_PAGE_SNAPSHOT_BYTES = 16 * 1024 * 1024
    DEFAULT_CANONICAL_RAW_PAGE_ITEMS = 1000

    # 用户生成目录（优先）
    USER_DIRS = {
        "00-Inbox",
        "02-Projects",
        "04-Concepts",
        "06-Retrospectives",
        "01-People",
        "05-MOCs",
    }

    def __init__(
        self,
        raw_projection_dir: Optional[str] = None,
        wiki_dir: Optional[str] = None,
        raw_events_db: Optional[str] = None,
        require_canonical_raw: bool = False,
        exclude_system: bool = True,
        exclude_external_content: bool = False,  # 改为 False：外部内容保留行为信号
    ):
        self.raw_projection_dir = Path(raw_projection_dir) if raw_projection_dir else None
        self.wiki_dir = Path(wiki_dir) if wiki_dir else None
        self.raw_events_db = Path(raw_events_db).expanduser() if raw_events_db else None
        self.require_canonical_raw = bool(
            require_canonical_raw or raw_events_db or raw_projection_dir
        )
        self.exclude_system = exclude_system
        self.exclude_external_content = exclude_external_content
        self._incremental_cursors: Dict[str, Dict[str, str]] = {}

    def read_all(self) -> Iterator[SourceItem]:
        """读取所有来源，自动过滤系统生成内容"""
        # Do not turn a multi-gigabyte canonical Raw database into one list
        # merely because a direct reader requests a full iterator.  The normal
        # Observation full path consumes explicit fair pages; this iterator
        # stays streaming for other declared readers as well.
        if self.raw_events_db is not None and self.raw_events_db.is_file():
            cursor: Dict[str, str] = {}
            while True:
                page = self._read_canonical_raw_db(
                    self.raw_events_db,
                    cursor=cursor,
                    limit=self.DEFAULT_CANONICAL_RAW_PAGE_ITEMS,
                    full_scan=False,
                )
                if not page:
                    break
                yield from page
                latest = page[-1].cursor_token
                if not self._cursor_allows(latest, cursor):
                    raise CanonicalRawUnavailable(
                        "canonical raw full reader did not advance its cursor"
                    )
                cursor = latest
            if self.wiki_dir and self.wiki_dir.exists():
                yield from self._read_wiki_dir(self.wiki_dir, since=None, cursor={})
            return
        if self.raw_events_db is not None and self.require_canonical_raw:
            raise CanonicalRawUnavailable(
                f"canonical raw database is unavailable: {self.raw_events_db}"
            )
        yield from self._read_sources(since=None, max_items=None, canonical_full_scan=True)

    def set_incremental_cursors(self, cursors: Dict[str, Dict[str, str]]) -> None:
        """Set durable per-source cursors for the next incremental page."""
        self._incremental_cursors = {
            str(source): {str(key): str(value) for key, value in token.items()}
            for source, token in cursors.items()
            if isinstance(token, dict)
        }

    def read_since(
        self,
        since: Optional[datetime] = None,
        max_items: Optional[int] = None,
        max_lookback_hours: Optional[int] = None,
    ) -> Iterator[SourceItem]:
        """
        增量读取：只返回 since 之后的新内容

        Args:
            since: 起始时间
            max_items: 最多返回的 item 数，防止长期停机后一次性吞入巨量数据
            max_lookback_hours: since 最大回退小时数，防止从太久以前开始扫描

        时间判断优先级：
        1. frontmatter 中的 date / created_at
        2. 文件修改时间（mtime）
        """
        yield from self._read_sources(
            since=since,
            max_items=max_items,
            max_lookback_hours=max_lookback_hours,
            canonical_full_scan=False,
        )

    def read_page(self, max_items: int) -> Iterator[SourceItem]:
        """Read one cursor-bounded fair page without applying a time cutoff.

        Full repair/reconciliation starts with empty durable cursors and then
        repeatedly calls this method.  It therefore covers all current Raw
        revisions and all Wiki files eventually, while each invocation remains
        bounded and cursor-resumable.
        """
        if max_items < 1:
            return
        yield from self._read_sources(
            since=None,
            max_items=max_items,
            max_lookback_hours=None,
            canonical_full_scan=False,
        )

    def _read_sources(
        self,
        *,
        since: Optional[datetime],
        max_items: Optional[int],
        max_lookback_hours: Optional[int] = None,
        canonical_full_scan: bool,
    ) -> Iterator[SourceItem]:
        """Read one fair page from each configured source stream.

        The old reader returned immediately after Raw reached ``max_items``,
        starving Wiki indefinitely.  Each source now contributes a bounded
        page and the returned page is round-robin interleaved.  Cursor tokens
        live on the individual items so the engine advances them only after a
        durable terminal result exists.
        """
        source_pages: List[List[SourceItem]] = []
        page_limit = max_items

        raw_page = self._read_raw_source(
            since=since,
            limit=page_limit,
            canonical_full_scan=canonical_full_scan,
        )
        if raw_page:
            source_pages.append(raw_page)

        wiki_since = since
        if wiki_since is not None and max_lookback_hours is not None and max_lookback_hours > 0:
            earliest = datetime.now(timezone.utc) - timedelta(hours=max_lookback_hours)
            if wiki_since < earliest:
                wiki_since = earliest
        if self.wiki_dir and self.wiki_dir.exists():
            wiki_page = list(
                itertools.islice(
                    self._read_wiki_dir(
                        self.wiki_dir,
                        since=wiki_since,
                        cursor=self._incremental_cursors.get("wiki", {}),
                    ),
                    page_limit,
                )
            )
            if wiki_page:
                source_pages.append(wiki_page)

        if not source_pages:
            return

        emitted = 0
        for index in itertools.count():
            emitted_this_round = False
            for page in source_pages:
                if index >= len(page):
                    continue
                yield page[index]
                emitted += 1
                emitted_this_round = True
                if max_items is not None and emitted >= max_items:
                    return
            if not emitted_this_round:
                return

    def _read_raw_source(
        self,
        *,
        since: Optional[datetime],
        limit: Optional[int],
        canonical_full_scan: bool,
    ) -> List[SourceItem]:
        if self.raw_events_db is not None:
            if not self.raw_events_db.is_file():
                if self.require_canonical_raw:
                    raise CanonicalRawUnavailable(
                        f"canonical raw database is unavailable: {self.raw_events_db}"
                    )
            else:
                return self._read_canonical_raw_db(
                    self.raw_events_db,
                    cursor=self._incremental_cursors.get("canonical_raw", {}),
                    limit=limit,
                    full_scan=canonical_full_scan,
                )
        if self.require_canonical_raw:
            raise CanonicalRawUnavailable("canonical raw database was not configured")
        return []

    @staticmethod
    def _parse_source_timestamp(value: str) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _cursor_allows(token: Dict[str, str], cursor: Dict[str, str]) -> bool:
        """Return whether one opaque ordered cursor token is after ``cursor``."""
        if not cursor:
            return True
        if set(token) != set(cursor):
            # A cursor shape mismatch means a producer contract changed.  Do
            # not reuse it as an approximate timestamp filter.
            return False
        if set(token) == {"updated_at", "event_id", "revision_id"}:
            keys: tuple[str, ...] = ("updated_at", "event_id", "revision_id")
        elif set(token) == {"mtime_ns", "path"}:
            keys = ("mtime_ns", "path")
        else:
            return False
        return tuple(token[key] for key in keys) > tuple(cursor[key] for key in keys)

    @staticmethod
    def _file_cursor_token(path: Path) -> Dict[str, str]:
        stat = path.stat()
        return {"mtime_ns": str(stat.st_mtime_ns), "path": str(path)}

    def _source_item_from_canonical_turn(self, turn: CanonicalRawTurn) -> SourceItem:
        timestamp_value = turn.conversation_at or turn.captured_at or turn.updated_at
        authority_context = dict(turn.authority_context or {})
        is_external = (
            authority_context.get("asset_kind") == "trusted_user_document"
            or authority_context.get("content_source") == ContentSource.EXTERNAL_FILE.value
            or authority_context.get("source_authority")
            in {"external_content", "quoted_content"}
        )
        has_user_content = bool(str(turn.user_content or "").strip())
        item = SourceItem(
            source_type="raw",
            file_path=f"raw://{turn.logical_event_id}/{turn.revision_id}",
            # Observation is a user-cognition consumer.  Canonical Raw still
            # stores the assistant bytes losslessly, but they cannot become a
            # user belief merely because both roles share one revision.
            content=str(turn.user_content or ""),
            frontmatter={
                "session_id": turn.session_id,
                "source": turn.source_agent,
                "date": timestamp_value,
                "created_at": turn.captured_at,
                "event_id": turn.logical_event_id,
                "revision_id": turn.revision_id,
                "content_hash": turn.content_hash,
                **authority_context,
            },
            content_tier=(
                ContentTier.EXTERNAL_QUOTED
                if is_external
                else (ContentTier.USER_GENERATED if has_user_content else ContentTier.SYSTEM_GENERATED)
            ),
            content_source=(
                ContentSource.EXTERNAL_FILE if is_external else ContentSource.NATIVE_DIALOGUE
            ),
            raw_event_id=turn.logical_event_id,
            raw_revision_id=turn.revision_id,
            raw_content_hash=turn.content_hash,
            source_content_hash=turn.content_hash,
            user_content=turn.user_content,
            assistant_content=turn.assistant_content,
            source_stream="canonical_raw",
            cursor_token=turn.cursor_token,
            session_id=turn.session_id,
            source_agent=turn.source_agent,
        )
        item.timestamp = self._parse_source_timestamp(timestamp_value)
        item.user_intent = self._infer_user_intent(item)
        return item

    def _read_canonical_raw_db(
        self,
        db_path: Path,
        *,
        cursor: Dict[str, str],
        limit: Optional[int],
        full_scan: bool,
    ) -> List[SourceItem]:
        """Read only canonical current Raw revisions; never parse a vault path.

        The public Raw read API validates current-revision, retention, alias,
        and native-contract visibility before any visible payload is handed to
        Observation.  An unavailable or malformed canonical store is a hard
        failure, never a reason to silently fall back to Markdown.
        """
        try:
            turns = list_current_raw_turns_readonly(
                db_path,
                cursor={} if full_scan else cursor,
                limit=limit,
                max_snapshot_bytes=self.MAX_CANONICAL_RAW_PAGE_SNAPSHOT_BYTES,
                include_structured_payload=False,
            )
        except CanonicalRawReadError as exc:
            raise CanonicalRawUnavailable(str(exc)) from exc

        items = [self._source_item_from_canonical_turn(turn) for turn in turns]
        session_lengths: Dict[str, List[int]] = {}
        for item in items:
            session_lengths.setdefault(item.session_id or "", []).append(
                len(item.user_content.strip())
            )
        session_stats = {
            session_id: self._calc_length_stats(lengths)
            for session_id, lengths in session_lengths.items()
        }
        for item in items:
            if item.content_tier == ContentTier.EXTERNAL_QUOTED:
                continue
            if item.content_tier == ContentTier.SYSTEM_GENERATED:
                continue
            item.content_source = self._detect_content_source(
                item, session_stats.get(item.session_id or "")
            )
            item.content_tier = (
                ContentTier.LIKELY_PASTED
                if item.content_source == ContentSource.LIKELY_PASTED
                else ContentTier.USER_GENERATED
            )
        return items

    def read_verified_raw_projection(self) -> Iterator[SourceItem]:
        """Verify a v2 Raw projection against canonical DB before exposing it.

        This is intentionally an explicit migration/audit reader rather than
        a runtime fallback.  The Markdown body is parsed by the same strict
        v2 fidelity parser used by the projection audit, while the visible
        values and source identities come from the current canonical Raw
        revision.  A path, heading, or loose regex can therefore never become
        a surrogate Raw event ID.
        """
        if not self.raw_projection_dir or not self.raw_events_db:
            raise CanonicalRawUnavailable(
                "Raw projection verification requires both projection_dir and raw_events.db"
            )
        try:
            current_turns = list_current_raw_turns_readonly(self.raw_events_db)
        except CanonicalRawReadError as exc:
            raise CanonicalRawUnavailable(str(exc)) from exc

        from scripts.audit_raw_projection_fidelity import _parse_projection_file
        from scripts.project_raw_vault import (
            PROJECTION_INDEX_MNEMOS_TYPE,
            PROJECTION_PART_PATH_PATTERN,
            VISIBLE_FIELDS,
            _sha256_text,
            managed_projection_paths,
            structured_field_text,
        )

        turns_by_revision = {turn.revision_id: turn for turn in current_turns}
        observed_revisions: set[str] = set()
        items: List[SourceItem] = []
        for relative_path in managed_projection_paths(self.raw_projection_dir):
            if PROJECTION_PART_PATH_PATTERN.search(relative_path):
                # Paged projection parts are consumed through their index page.
                continue
            path = self.raw_projection_dir / relative_path
            parsed, parse_errors, _has_truncation_marker = _parse_projection_file(path)
            if parse_errors:
                raise CanonicalRawUnavailable(
                    f"Raw Markdown v2 parser rejected {path}: {parse_errors[0]}"
                )
            projection = self._parse_markdown(path, source_type="raw")
            if projection is None:
                raise CanonicalRawUnavailable(f"Raw Markdown projection is unreadable: {path}")
            frontmatter = projection.frontmatter
            if (
                frontmatter.get("mnemos_type")
                not in {"raw_retention_projection", PROJECTION_INDEX_MNEMOS_TYPE}
                or frontmatter.get("projection_version") != 2
                or frontmatter.get("projection_contract") != "lossless-visible-v1"
            ):
                raise CanonicalRawUnavailable(f"Raw Markdown projection contract is invalid: {path}")
            frontmatter_event_ids = {
                str(value) for value in frontmatter.get("event_ids", []) if value
            }
            source_agent = str(frontmatter.get("source") or "")
            session_id = str(frontmatter.get("session_id") or "")
            for revision_id, record in parsed.items():
                turn = turns_by_revision.get(revision_id)
                if turn is None:
                    raise CanonicalRawUnavailable(
                        f"Raw Markdown references a non-current canonical revision: {revision_id}"
                    )
                if revision_id not in frontmatter_event_ids:
                    raise CanonicalRawUnavailable(
                        f"Raw Markdown frontmatter omits parsed revision: {revision_id}"
                    )
                marker = record.get("marker") if isinstance(record, dict) else None
                if not isinstance(marker, dict) or (
                    str(marker.get("logical_event_id") or "") != turn.logical_event_id
                ):
                    raise CanonicalRawUnavailable(
                        f"Raw Markdown event identity mismatches canonical revision: {revision_id}"
                    )
                if source_agent != turn.source_agent or session_id != turn.session_id:
                    raise CanonicalRawUnavailable(
                        f"Raw Markdown frontmatter identity mismatches canonical revision: {revision_id}"
                    )
                expected_values = {
                    "user_content": turn.user_content,
                    "assistant_content": turn.assistant_content,
                    "reasoning": turn.reasoning,
                    "structured": "",
                }
                # The read-only observation contract consumes user/assistant
                # text, but verifying all v2 visible fields prevents a partial
                # projection from masquerading as compatible input.
                try:
                    raw_turn = {
                        "user_content": turn.user_content,
                        "assistant_content": turn.assistant_content,
                        "reasoning": turn.reasoning,
                        "tool_calls": turn.tool_calls,
                        "tool_results": turn.tool_results,
                        "attachments": turn.attachments,
                        "raw_event_refs": turn.raw_event_refs,
                        "source_files": turn.source_files,
                    }
                    expected_values["structured"] = structured_field_text(raw_turn)
                    fields = record.get("fields") if isinstance(record, dict) else None
                    marker_hashes = marker.get("field_hashes")
                    if not isinstance(fields, dict) or not isinstance(marker_hashes, dict):
                        raise ValueError("field metadata missing")
                    for field_name in VISIBLE_FIELDS:
                        field_record = fields.get(field_name)
                        if not isinstance(field_record, dict):
                            raise ValueError(f"missing field {field_name}")
                        expected_hash = _sha256_text(expected_values[field_name])
                        field_marker = field_record.get("marker")
                        if (
                            field_record.get("content_hash") != expected_hash
                            or not isinstance(field_marker, dict)
                            or field_marker.get("sha256") != expected_hash
                            or marker_hashes.get(field_name) != expected_hash
                        ):
                            raise ValueError(f"field hash mismatch for {field_name}")
                except (TypeError, ValueError, KeyError) as exc:
                    raise CanonicalRawUnavailable(
                        f"Raw Markdown visible-field mismatch for {revision_id}: {exc}"
                    ) from exc
                item = self._source_item_from_canonical_turn(turn)
                item.source_stream = "verified_raw_projection"
                item.cursor_token = {}
                items.append(item)
                observed_revisions.add(revision_id)

        expected_revisions = set(turns_by_revision)
        if observed_revisions != expected_revisions:
            missing = sorted(expected_revisions - observed_revisions)
            unexpected = sorted(observed_revisions - expected_revisions)
            raise CanonicalRawUnavailable(
                "Raw projection denominator differs from canonical Raw: "
                f"missing={missing[:3]} unexpected={unexpected[:3]}"
            )
        yield from sorted(items, key=lambda item: item.raw_revision_id)

    def _is_file_new(self, md_file: Path, item: SourceItem, since: Optional[datetime]) -> bool:
        """判断文件是否在 since 之后（增量过滤）"""
        if since is None:
            return True

        # 统一转为 aware UTC 时间戳再比较，避免 aware/naive 混用报错
        def _to_utc_ts(dt: datetime) -> float:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc).timestamp()
            return dt.astimezone(timezone.utc).timestamp()

        since_ts = _to_utc_ts(since)

        # 优先用 frontmatter 时间
        if item.timestamp:
            try:
                if _to_utc_ts(item.timestamp) >= since_ts:
                    return True
            except (ValueError, TypeError, OSError):
                # 时间戳解析失败时降级到文件 mtime
                pass

        # fallback 到文件修改时间
        try:
            if md_file.stat().st_mtime >= since_ts:
                return True
        except OSError:
            logger.warning("[sources] OSError suppressed", exc_info=True)

        return False

    def _read_wiki_dir(
        self,
        root: Path,
        since: Optional[datetime] = None,
        cursor: Optional[Dict[str, str]] = None,
    ) -> Iterator[SourceItem]:
        """读取 wiki 目录，带分层过滤"""
        cursor = cursor or {}
        candidates: List[SourceItem] = []
        for md_file in sorted(root.rglob("*.md")):
            if any(part.startswith(".") for part in md_file.parts):
                continue
            if md_file.name == "index.md":
                continue

            item = self._parse_markdown(md_file, source_type="wiki")
            if not item:
                continue

            # 增量过滤
            if not self._is_file_new(md_file, item, since):
                continue

            try:
                item.cursor_token = self._file_cursor_token(md_file)
            except OSError:
                logger.warning("[sources] wiki markdown stat failed: %s", md_file)
                continue
            if not self._cursor_allows(item.cursor_token, cursor):
                continue
            item.source_stream = "wiki"

            # wiki 文件的内容来源：从 frontmatter 推断
            item.content_source = self._detect_wiki_content_source(item)
            item.user_intent = self._detect_wiki_user_intent(item)

            # 来源必须先恢复，再决定认知层级；否则 00-Inbox 中的外部页
            # 会被目录规则误判成用户原创。
            item.content_tier = self._classify_content_tier(item, md_file)

            if self._should_include(item):
                candidates.append(item)

        yield from sorted(
            candidates,
            key=lambda item: (
                item.cursor_token.get("mtime_ns", ""),
                item.cursor_token.get("path", ""),
            ),
        )

    def _extract_user_message_lengths(self, text: str) -> List[int]:
        """从 raw 文件中提取用户消息的长度"""
        lengths = []
        # 匹配 **User** (claude): 后的内容
        # 格式: **User** (agent):\n\ncontent\n\n**Assistant**:
        user_blocks = re.findall(
            r"\*\*User\*\*\s*\([^)]*\):\s*\n\n(.*?)\n\n\*\*Assistant\*\*", text, re.DOTALL
        )
        # The current projection renderer also emits heading-based user blocks.
        # They remain non-canonical unless separately verified against Raw.
        if not user_blocks:
            user_blocks = re.findall(
                r"^### User\s*\n\n(.*?)(?=\n\n### Assistant\s*$)",
                text,
                re.DOTALL | re.MULTILINE,
            )
        for block in user_blocks:
            # 去掉 tool results 等噪音
            clean = re.sub(r"## Tool Results.*?```", "", block, flags=re.DOTALL)
            clean = re.sub(r"## Reasoning.*?Checksum:", "", clean, flags=re.DOTALL)
            lengths.append(len(clean.strip()))
        return lengths

    def _calc_length_stats(self, lengths: List[int]) -> Dict:
        """计算字数分布统计量"""
        if not lengths:
            return {"p50": 0, "p90": 0, "p99": 0, "count": 0}
        sorted_lens = sorted(lengths)
        n = len(sorted_lens)
        p50 = sorted_lens[n // 2]
        p90 = sorted_lens[int(n * 0.9)] if n >= 10 else sorted_lens[-1]
        p99 = sorted_lens[int(n * 0.99)] if n >= 100 else sorted_lens[-1]
        return {"p50": p50, "p90": p90, "p99": max(p99, 100), "count": n}

    def _detect_content_source(self, item: SourceItem, stats: Optional[Dict]) -> ContentSource:
        """检测内容来源（raw 文件）"""
        fm = item.frontmatter

        # 1. 用户明确标记为自己写的
        if fm.get("content_source") == "user_note" or fm.get("作者") == "user":
            return ContentSource.USER_NOTE

        # 2. 外部文件导入标记
        if fm.get("content_source") == "external_file":
            return ContentSource.EXTERNAL_FILE

        # 3. 复制粘贴检测
        if stats and stats["count"] >= 5:
            user_lengths = (
                [len(item.user_content.strip())]
                if item.raw_revision_id
                else self._extract_user_message_lengths(item.content)
            )
            if user_lengths:
                max_len = max(user_lengths)
                if max_len > stats["p99"] and max_len > 300:
                    return ContentSource.LIKELY_PASTED

        # 4. 默认：原生对话
        return ContentSource.NATIVE_DIALOGUE

    def _detect_wiki_content_source(self, item: SourceItem) -> ContentSource:
        """检测 wiki 文件的内容来源"""
        fm = item.frontmatter

        behavior_source = fm_get(fm, "behavior_content_source")
        if behavior_source in {source.value for source in ContentSource}:
            return ContentSource(str(behavior_source))

        # 从 frontmatter 中的来源字段推断
        source = fm.get("来源") or fm.get("source") or ""
        if source in ("claude", "kimi", "codex"):
            return ContentSource.NATIVE_DIALOGUE
        if source in ("user", "human"):
            return ContentSource.USER_NOTE
        if fm.get("content_source") == "external_file":
            return ContentSource.EXTERNAL_FILE

        # 从内容分层推断
        if item.content_tier == ContentTier.EXTERNAL_QUOTED:
            return ContentSource.EXTERNAL_FILE

        return ContentSource.UNKNOWN

    def _detect_wiki_user_intent(self, item: SourceItem) -> UserIntent:
        """从蒸馏 Wiki frontmatter 恢复用户行为/意图信号。"""
        fm = item.frontmatter
        signal = fm_get(fm, "user_intent_signal")
        if signal in {intent.value for intent in UserIntent}:
            return UserIntent(str(signal))

        hypothesis = fm_get(fm, "intent_hypothesis")
        if hypothesis in {intent.value for intent in UserIntent}:
            return UserIntent(str(hypothesis))
        if hypothesis == "curate_or_decision_material":
            return UserIntent.CURATE_OR_DECISION_MATERIAL

        return UserIntent.UNKNOWN

    def _infer_user_intent(self, item: SourceItem) -> UserIntent:
        """推断用户意图（基于消息内容）"""
        if item.user_content:
            user_text = item.user_content.lower()
        else:
            # Formatted Wiki/projection sources expose user blocks in content;
            # canonical DB-backed items always use the exact field above.
            user_blocks = re.findall(
                r"\*\*User\*\*\s*\([^)]*\):\s*\n\n(.*?)\n\n\*\*Assistant\*\*",
                item.content,
                re.DOTALL,
            )
            if not user_blocks:
                user_blocks = re.findall(
                    r"^### User\s*\n\n(.*?)(?=\n\n### Assistant\s*$)",
                    item.content,
                    re.DOTALL | re.MULTILINE,
                )
            if not user_blocks:
                return UserIntent.UNKNOWN
            user_text = user_blocks[-1].lower()

        if not user_text.strip():
            return UserIntent.UNKNOWN

        # 判断意图
        # 1. 请判断/评估
        if re.search(r"(觉得|认为|评价|判断|分析|怎么看|行不行|可行|靠谱|值得)", user_text):
            return UserIntent.SEEKING_JUDGMENT

        # 2. 请总结/提炼
        if re.search(r"(总结|提炼|概括|摘要|精简|整理)", user_text):
            return UserIntent.SEEKING_SUMMARY

        # 3. 表达认同
        if re.search(r"(同意|赞同|说得对|有道理|没错|正是|确实)", user_text):
            return UserIntent.EXPRESSING_AGREEMENT

        # 4. 表达质疑
        if re.search(r"(不对|错了|有问题|质疑|反对|不同意|但是|不过)", user_text):
            return UserIntent.EXPRESSING_DOUBT

        # 5. 基于内容提问
        if re.search(r"(为什么|怎么|如何|什么|哪|吗|呢)[？?]", user_text):
            return UserIntent.ASKING_QUESTION

        # 6. 单纯分享（大段内容 + 没有明确请求）
        if len(user_text) > 500 and not re.search(r"(请|帮我|给我|能否|可以|建议)", user_text):
            return UserIntent.SHARING_INFORMATION

        return UserIntent.UNKNOWN

    def _classify_content_tier(self, item: SourceItem, file_path: Path) -> ContentTier:
        """判断内容分层"""
        fm = item.frontmatter
        relative_parts = set()
        if self.wiki_dir and file_path.is_relative_to(self.wiki_dir):
            relative_parts = set(file_path.relative_to(self.wiki_dir).parts[:1])

        # 1. 系统生成判断
        if fm.get("source") == "system":
            return ContentTier.SYSTEM_GENERATED
        if fm.get("type") == "retrospective-reminder":
            return ContentTier.SYSTEM_GENERATED
        if fm.get("auto_updated") is True and fm.get("mnemos_type") == "dashboard":
            return ContentTier.SYSTEM_GENERATED
        if relative_parts & self.SYSTEM_DIRS:
            return ContentTier.SYSTEM_GENERATED

        # 2. 外部引用判断
        if fm_get(fm, "cognitive_authority_status") == "pending_hypothesis":
            return ContentTier.EXTERNAL_QUOTED
        if item.content_source == ContentSource.EXTERNAL_FILE:
            return ContentTier.EXTERNAL_QUOTED
        if relative_parts & self.EXTERNAL_DIRS:
            return ContentTier.EXTERNAL_QUOTED
        if fm.get("source_type") in ("external", "book", "news", "paper"):
            return ContentTier.EXTERNAL_QUOTED
        if fm.get("来源类型") in ("外部", "书籍", "新闻", "文献"):
            return ContentTier.EXTERNAL_QUOTED

        # 3. 复制粘贴判断（已由 _detect_content_source 处理，这里同步）
        if item.content_source == ContentSource.LIKELY_PASTED:
            return ContentTier.LIKELY_PASTED

        # 4. 用户生成判断
        if fm.get("来源") in ("claude", "kimi", "codex", "user"):
            return ContentTier.USER_GENERATED
        if relative_parts & self.USER_DIRS:
            return ContentTier.USER_GENERATED

        # 5. 默认：未知，保守进入 L3
        return ContentTier.UNKNOWN

    def _should_include(self, item: SourceItem) -> bool:
        """判断是否应该包含在 Observation 提取中"""
        # 系统生成内容：完全排除
        if self.exclude_system and item.content_tier == ContentTier.SYSTEM_GENERATED:
            return False

        # 外部引用内容：不 exclude，但会在提取器层面限制（只进 Attention）
        # if self.exclude_external_content and item.content_tier == ContentTier.EXTERNAL_QUOTED:
        #     return False

        return True

    def _parse_markdown(self, file_path: Path, source_type: str) -> Optional[SourceItem]:
        """解析单个 markdown 文件"""
        try:
            text = file_path.read_text(encoding="utf-8")
        # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        except (
            OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
        ):
            return None

        frontmatter: Dict[str, Any] = {}
        content = text

        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    frontmatter = yaml.safe_load(parts[1]) or {}
                    content = parts[2].strip()
                except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                    logger.debug("解析 frontmatter 失败", exc_info=True)

        return SourceItem(
            source_type=source_type,
            file_path=str(file_path),
            content=content,
            frontmatter=frontmatter,
        )

    def _read_file_safe(self, file_path: Path) -> Optional[str]:
        """安全读取文件内容"""
        try:
            return file_path.read_text(encoding="utf-8")
        # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        except (
            OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
        ):
            return None

    def get_stats(self) -> Dict:
        """获取数据源统计（含分层）"""
        stats: Dict[str, Any] = {
            "raw_items": 0,
            "raw_projection_files": 0,
            "wiki_files": 0,
            "total_items": 0,
            "raw_projection_dir": (
                str(self.raw_projection_dir) if self.raw_projection_dir else None
            ),
            "raw_events_db": str(self.raw_events_db) if self.raw_events_db else None,
            "wiki_dir": str(self.wiki_dir) if self.wiki_dir else None,
            "tier_breakdown": {},
            "source_breakdown": {},
            "intent_breakdown": {},
            "excluded_system": 0,
            "excluded_external": 0,
        }

        # Canonical Raw is the L1 denominator.  Projection files are reported
        # separately and never counted as cognitive source items.
        if self.raw_events_db:
            try:
                stats["raw_items"] = count_current_raw_turns_readonly(self.raw_events_db)
            except CanonicalRawReadError as exc:
                stats["canonical_raw_error"] = str(exc)
        if self.raw_projection_dir and self.raw_projection_dir.exists():
            stats["raw_projection_files"] = sum(
                1
                for f in self.raw_projection_dir.rglob("*.md")
                if not any(p.startswith(".") for p in f.parts)
            )

        # 统计 wiki（含分层）
        if self.wiki_dir and self.wiki_dir.exists():
            for md_file in self.wiki_dir.rglob("*.md"):
                if any(p.startswith(".") for p in md_file.parts):
                    continue
                if md_file.name == "index.md":
                    continue

                item = self._parse_markdown(md_file, source_type="wiki")
                if item:
                    tier = self._classify_content_tier(item, md_file)
                    stats["tier_breakdown"][tier.value] = (
                        stats["tier_breakdown"].get(tier.value, 0) + 1
                    )
                    if tier == ContentTier.SYSTEM_GENERATED:
                        stats["excluded_system"] += 1
                    elif tier == ContentTier.EXTERNAL_QUOTED:
                        stats["excluded_external"] += 1
                    else:
                        stats["wiki_files"] += 1

        stats["total_items"] = stats["raw_items"] + stats["wiki_files"]
        return stats
