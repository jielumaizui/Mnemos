# -*- coding: utf-8 -*-
"""
Wiki Metrics - 精简版 wiki 质量与热力追踪
合并自：wiki_heat_tracker + wiki_quality + quality_assessor + quality_filter + tiered_filter
功能：
1. 页面元数据追踪（completeness, freshness, backlinks, source_count）
2. 知识阶段（P0-P3）和证据等级（1-4）
3. 简化热力系统（3级：cold/warm/hot）
4. 页面关系索引（供 curator 合并决策）
存储：~/.mnemos/wiki_metrics.db
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from functools import wraps
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, List, Dict, Optional

from core.config import get_config
from core.cognitive.state_contract import sha256_json
from core.db_utils import delete_older_than, validate_sql_identifier, _should_force_transient_pool
from core.frontmatter import (
    fm_get,
    to_chinese_frontmatter_preserving_unknown,
    write_frontmatter,
)
from core.kia.relation_endpoint_quality import is_derived_kg_scan_path
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    submit_or_write_markdown_with_decision,
)
from core.trust.markdown_adapter import read_markdown_text
from core.trust.models import sha256_text
from core.wiki_page_roles import classify_wiki_page_role
from core.wiki_metrics_lifecycle import WikiMetricsLifecycleMixin

logger = logging.getLogger(__name__)

WIKI_METRICS_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:wiki-metrics-projection",
    contract_revision_id="mnemos.wiki_metrics_projection.v1",
    contract_text=(
        "WikiMetrics may project only the exact metrics snapshot, heat report, or "
        "home dashboard computed from its exact configured state and page preimage."
    ),
    source_namespace="wiki-metrics-projection",
    producer="wiki-metrics",
    producer_code_hash=sha256_json(
        {
            "module": "core.wiki_metrics",
            "producers": [
                "update_page_frontmatter",
                "generate_heat_report",
                "write_mnemos_home",
            ],
            "version": "mnemos.wiki_metrics_projection.v1",
        }
    ),
    evaluator_id="wiki-metrics-projection-evaluator",
    constraints=(
        "Metrics database, target, page preimage, computed fields, and output remain exact.",
        "A report without a source denominator may not be written as formal evidence.",
    ),
    approved_candidate_key="project_exact_wiki_metrics_state",
    approved_candidate_summary="Project the exact computed Wiki metrics state.",
    rejected_candidate_key="retain_wiki_without_metrics_projection",
    rejected_candidate_summary="Retain Wiki state when metrics evidence or bytes drift.",
    approved_reason_code="wiki_metrics_binding_verified",
    rejected_reason_code="wiki_metrics_binding_rejected",
    committed_metric="wiki_metrics_projection_committed",
    rejected_metric="unbound_wiki_metrics_projection_count",
)

from core.wiki_metrics_contract import (  # noqa: F401
    DB_PATH,
    WIKI_DIR,
    HeatLevel,
    KnowledgeStage,
    PageMetrics,
    QualityLevel,
    WIKI_METRICS_CATEGORY_DECAY_DAYS,
    WIKI_METRICS_CATEGORY_DECAY_DAYS_2,
    WIKI_METRICS_DURATION_BUCKET_MONTH_DAYS,
    _heat_level_to_display,
    _stage_to_display,
    _status_to_display,
    _utcnow,
    compute_evidence_level,
    compute_heat_level,
    compute_knowledge_stage,
    hash_query,
    quick_quality_score,
)

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


# ==================== 1. 枚举 ====================


# ==================== 5. WikiMetrics ====================


class WikiMetrics(WikiMetricsLifecycleMixin):
    """
    Wiki 度量中心

    单一数据库存储所有 wiki 页面的质量、热力和阶段信息。
    """

    _score_content = staticmethod(quick_quality_score)
    _classify_page_role = staticmethod(classify_wiki_page_role)

    CATEGORY_DECAY_DAYS = {
        "technology": WIKI_METRICS_CATEGORY_DECAY_DAYS,
        "methodology": WIKI_METRICS_CATEGORY_DECAY_DAYS_2,
        "practice": 60,
    }

    def __getattribute__(self, name: str):
        attr = object.__getattribute__(self, name)
        if name.startswith("_") or name == "close" or not callable(attr):
            return attr
        class_attr = getattr(type(self), name, None)
        if not callable(class_attr):
            return attr
        try:
            transient = object.__getattribute__(self, "_transient_sqlite")
        except AttributeError:
            return attr
        if not transient:
            return attr

        @wraps(attr)
        def release_after_call(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            finally:
                self._release_transient_connections()

        return release_after_call

    def __init__(self, db_path: Optional[str] = None, wiki_dir: Optional[str] = None):
        self._db_path: Optional[Path]
        if db_path is not None:
            self._db_path = Path(db_path)
        elif wiki_dir:
            configured_wiki = Path(get_config().wiki_dir).expanduser().resolve(
                strict=False
            )
            requested_wiki = Path(wiki_dir).expanduser().resolve(strict=False)
            self._db_path = (
                requested_wiki / ".kg" / "wiki_metrics.db"
                if requested_wiki != configured_wiki
                else None
            )
        else:
            self._db_path = None  # 使用 LazyPath
        self._wiki_dir = Path(wiki_dir).expanduser() if wiki_dir else None
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all_conns: set[sqlite3.Connection] = set()
        self._transient_conns: set[sqlite3.Connection] = set()
        self._transient_sqlite = _should_force_transient_pool(self.db_path)
        self._init_db()
        self._release_transient_connections()

    @property
    def db_path(self) -> Path:
        if self._db_path is not None:
            return self._db_path
        return Path(str(DB_PATH))

    @property
    def wiki_dir(self) -> Path:
        if self._wiki_dir is not None:
            return self._wiki_dir
        return Path(str(WIKI_DIR))

    def _open_conn(self) -> sqlite3.Connection:
        db = self.db_path
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row  # noqa
        return conn

    def _get_conn(self) -> sqlite3.Connection:
        if self._transient_sqlite:
            conn = self._open_conn()
            transient_conns = getattr(self._local, "transient_conns", None)
            if transient_conns is None:
                transient_conns = []
                self._local.transient_conns = transient_conns
            transient_conns.append(conn)
            with self._lock:
                self._transient_conns.add(conn)
            return conn
        if not hasattr(self._local, "conn") or self._local.conn is None:
            with self._lock:
                if not hasattr(self._local, "conn") or self._local.conn is None:
                    self._local.conn = self._open_conn()
                    self._all_conns.add(self._local.conn)
        return self._local.conn  # type: ignore[no-any-return]

    @staticmethod
    def _close_conn(conn: sqlite3.Connection) -> None:
        try:
            conn.close()
        except (sqlite3.Error, OSError):
            logger.warning("WikiMetrics 关闭 SQLite 连接失败", exc_info=True)

    def _release_transient_connections(self) -> None:
        if not getattr(self, "_transient_sqlite", False):
            return
        conns = list(getattr(self._local, "transient_conns", []))
        self._local.transient_conns = []
        with self._lock:
            for conn in conns:
                self._transient_conns.discard(conn)
        for conn in conns:
            self._close_conn(conn)

    def close(self) -> None:
        self._release_transient_connections()
        with self._lock:
            conns = list(self._all_conns) + list(self._transient_conns)
            self._all_conns.clear()
            self._transient_conns.clear()
        self._local.conn = None
        self._local.transient_conns = []
        for conn in conns:
            self._close_conn(conn)

    def __enter__(self) -> "WikiMetrics":
        return self

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        self.close()

    def _init_db(self):
        """初始化数据库"""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS page_metrics (
                wiki_path TEXT PRIMARY KEY,
                title TEXT,
                page_role TEXT DEFAULT 'knowledge',
                knowledge_stage TEXT DEFAULT 'P3',
                evidence_level INTEGER DEFAULT 1,
                source_count INTEGER DEFAULT 0,
                source_refs TEXT DEFAULT '[]',
                heat_level TEXT DEFAULT 'cold',
                heat_score REAL DEFAULT 0.0,
                quality_score REAL DEFAULT 0.0,
                quality_level TEXT DEFAULT 'acceptable',
                completeness REAL DEFAULT 0.0,
                freshness_days INTEGER DEFAULT 999,
                backlink_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'draft',
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_accessed TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tags TEXT DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS page_relations (
                from_path TEXT,
                to_path TEXT,
                relation_type TEXT DEFAULT 'link',
                strength REAL DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (from_path, to_path)
            );
            CREATE INDEX IF NOT EXISTS idx_rel_to
                ON page_relations(to_path);
            CREATE TABLE IF NOT EXISTS query_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_hash TEXT,
                query_text TEXT,
                matched_pages TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(page_metrics)").fetchall()}
        expected_columns = {
            "title": "TEXT",
            "page_role": "TEXT DEFAULT 'knowledge'",
            "knowledge_stage": "TEXT DEFAULT 'P3'",
            "evidence_level": "INTEGER DEFAULT 1",
            "source_count": "INTEGER DEFAULT 0",
            "source_refs": "TEXT DEFAULT '[]'",
            "heat_level": "TEXT DEFAULT 'cold'",
            "heat_score": "REAL DEFAULT 0.0",
            "quality_score": "REAL DEFAULT 0.0",
            "quality_level": "TEXT DEFAULT 'acceptable'",
            "completeness": "REAL DEFAULT 0.0",
            "freshness_days": "INTEGER DEFAULT 999",
            "backlink_count": "INTEGER DEFAULT 0",
            "status": "TEXT DEFAULT 'draft'",
            "last_updated": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "last_accessed": "TIMESTAMP",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "tags": "TEXT DEFAULT '[]'",
        }
        for column, ddl in expected_columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE page_metrics ADD COLUMN {column} {ddl}")
        conn.commit()

    # ---- 页面操作 ----

    def upsert_page(self, path: str, **kwargs):
        """插入或更新页面指标"""
        path = self._resolve_existing_key(path) or self._canonical_metric_path(path)
        preserve_last_updated = bool(kwargs.pop("_preserve_last_updated", False))
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM page_metrics WHERE wiki_path = ?", (path,)).fetchone()

        if row:
            # 更新：只更新提供的字段
            allowed = [
                "title",
                "page_role",
                "knowledge_stage",
                "evidence_level",
                "source_count",
                "source_refs",
                "heat_level",
                "heat_score",
                "quality_score",
                "quality_level",
                "completeness",
                "freshness_days",
                "backlink_count",
                "status",
                "last_updated",
                "last_accessed",
                "tags",
            ]
            updates = []
            values = []
            for k, v in kwargs.items():
                if k in allowed:
                    if k in ("source_refs", "tags") and isinstance(v, list):
                        v = json.dumps(v, ensure_ascii=False)
                    updates.append(f"{validate_sql_identifier(k)} = ?")
                    values.append(v)
            if updates:
                if "last_updated" not in kwargs and not preserve_last_updated:
                    updates.append("last_updated = ?")
                    values.append(_utcnow().isoformat())
                values.append(path)
                conn.execute(
                    f"UPDATE page_metrics SET {', '.join(updates)} WHERE wiki_path = ?",  # nosec B608
                    values,
                )
                conn.commit()
        else:
            # 插入新记录
            defaults = {
                "title": "",
                "page_role": "knowledge",
                "knowledge_stage": "P3",
                "evidence_level": 1,
                "source_count": 0,
                "source_refs": "[]",
                "heat_level": "cold",
                "heat_score": 0.0,
                "quality_score": 0.0,
                "quality_level": "acceptable",
                "completeness": 0.0,
                "freshness_days": 999,
                "backlink_count": 0,
                "status": "draft",
                "tags": "[]",
                "last_updated": _utcnow().isoformat(),
                "last_accessed": None,
                "created_at": _utcnow().isoformat(),
            }
            defaults.update(kwargs)
            for k in ("source_refs", "tags"):
                if isinstance(defaults.get(k), list):
                    defaults[k] = json.dumps(defaults[k], ensure_ascii=False)

            conn.execute(
                """
                INSERT INTO page_metrics
                (wiki_path, title, page_role, knowledge_stage, evidence_level, source_count,
                 source_refs, heat_level, heat_score, quality_score, quality_level,
                 completeness, freshness_days, backlink_count, status,
                 last_updated, last_accessed, created_at, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    path,
                    defaults["title"],
                    defaults["page_role"],
                    defaults["knowledge_stage"],
                    defaults["evidence_level"],
                    defaults["source_count"],
                    defaults["source_refs"],
                    defaults["heat_level"],
                    defaults["heat_score"],
                    defaults["quality_score"],
                    defaults["quality_level"],
                    defaults["completeness"],
                    defaults["freshness_days"],
                    defaults["backlink_count"],
                    defaults["status"],
                    defaults["last_updated"],
                    defaults["last_accessed"],
                    defaults["created_at"],
                    defaults["tags"],
                ),
            )
            conn.commit()

    def cleanup_older_than(self, days: int, dry_run: bool = False) -> int:
        """清理/统计 query_log 中 created_at 早于保留期限的记录。"""
        conn = self._get_conn()
        return delete_older_than(conn, "query_log", "created_at", days, dry_run=dry_run)

    def _parse_page_frontmatter(self, content: str, rel_path: str) -> tuple:
        """从 frontmatter 解析 title/status/tags/knowledge_stage（保留现有映射）。"""
        import yaml

        title = Path(rel_path).stem
        status = "draft"
        tags: List[str] = []
        knowledge_stage = "P3"

        if not content.startswith("---"):
            return title, status, tags, knowledge_stage

        parts = content.split("---", 2)
        if len(parts) < 3:
            return title, status, tags, knowledge_stage

        try:
            fm = yaml.safe_load(parts[1]) or {}
        except (ValueError, KeyError, TypeError):
            logger.debug("frontmatter 解析失败: %s", rel_path, exc_info=True)
            return title, status, tags, knowledge_stage

        title = fm_get(fm, "title", title)
        status_map = {
            "草稿": "draft",
            "活跃": "active",
            "已验证": "verified",
            "待审": "review",
            "待验证": "pending-verification",
            "已合并": "merged",
            "废弃": "deprecated",
        }
        status = status_map.get(fm.get("状态", ""), "draft")
        tags = fm.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        elif not isinstance(tags, list):
            tags = []
        stage_map = {
            "核心": "P0",
            "已验证": "P0",
            "成熟": "P1",
            "发展中": "P2",
            "初筛": "P2",
            "已整理": "P2",
            "原始": "P3",
        }
        knowledge_stage = stage_map.get(fm.get("知识阶段", ""), "P3")
        return title, status, tags, knowledge_stage

    @staticmethod
    def _compute_quality_level(quality_score: float) -> str:
        if quality_score >= 80:
            return QualityLevel.EXCELLENT.value
        if quality_score >= 60:
            return QualityLevel.GOOD.value
        if quality_score >= 40:
            return QualityLevel.ACCEPTABLE.value
        return QualityLevel.POOR.value

    def _build_page_payload(
        self, content: str, stat, existing_metrics: Optional[PageMetrics]
    ) -> Dict[str, Any]:
        """计算并返回 page_metrics 更新所需 payload。"""
        quality_score = quick_quality_score(content)
        quality_level = self._compute_quality_level(quality_score)
        last_updated = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        freshness_days = max(
            0, (_utcnow() - datetime.fromtimestamp(stat.st_mtime, timezone.utc)).days
        )
        heat_level = compute_heat_level(last_updated)
        heat_score = {"hot": 3.0, "warm": 1.0, "cold": 0.0}.get(heat_level, 0.0)
        completeness = min(1.0, quality_score / 100)
        return {
            "quality_score": round(quality_score, 1),
            "quality_level": quality_level,
            "freshness_days": freshness_days,
            "heat_level": compute_heat_level(
                last_updated,
                existing_metrics.last_accessed if existing_metrics else None,
            ),
            "heat_score": (
                max(existing_metrics.heat_score, heat_score) if existing_metrics else heat_score
            ),
            "completeness": round(completeness, 2),
            "last_updated": last_updated,
        }

    def _upsert_scanned_page(self, rel_path: str, payload: Dict[str, Any]) -> tuple[int, int]:
        """判断 INSERT/UPDATE 并返回 (inserted, updated) 计数。"""
        row = (
            self._get_conn()
            .execute("SELECT 1 FROM page_metrics WHERE wiki_path = ?", (rel_path,))
            .fetchone()
        )
        self.upsert_page(rel_path, **payload)
        if row:
            return 0, 1
        return 1, 0

    def _prune_removed_pages(self, seen_paths: set) -> int:
        """删除不在 seen_paths 中的 page_metrics 行。"""
        if not seen_paths:
            return 0
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in seen_paths)
        cursor = conn.execute(
            f"DELETE FROM page_metrics WHERE wiki_path NOT IN ({placeholders})",  # nosec B608
            tuple(seen_paths),
        )
        conn.commit()
        return cursor.rowcount

    def scan_all_pages(self) -> Dict[str, int]:
        """全量扫描 Wiki 目录，为所有页面创建/更新 metrics"""
        wiki = self.wiki_dir
        if not wiki.exists():
            return {"total": 0, "inserted": 0, "updated": 0}

        inserted = 0
        updated = 0
        seen_paths = set()
        for md_file in wiki.rglob("*.md"):
            try:
                rel_path = str(md_file.relative_to(wiki))
                if any(part.startswith(".") for part in Path(rel_path).parts):
                    continue
                seen_paths.add(rel_path)
                ins, upd = self._refresh_page_file(md_file, rel_path)
                inserted += ins
                updated += upd

                try:
                    self.sync_heat_to_frontmatter(md_file)
                except (OSError, ValueError, TypeError):
                    logger.debug("frontmatter sync failed for %s", md_file, exc_info=True)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                logger.debug("Wiki metrics sync failed for file, skipping", exc_info=True)
                continue

        deleted = self._prune_removed_pages(seen_paths)

        return {
            "total": inserted + updated,
            "inserted": inserted,
            "updated": updated,
            "deleted": deleted,
        }

    def _resolve_existing_key(self, path: str) -> Optional[str]:
        """找到已存在的 metrics key。"""
        conn = self._get_conn()
        for candidate in self._path_candidates(path):
            row = conn.execute(
                "SELECT wiki_path FROM page_metrics WHERE wiki_path = ?",
                (candidate,),
            ).fetchone()
            if row:
                return row[0]  # type: ignore[no-any-return]
        return None

    def get_page(self, path: str) -> Optional[PageMetrics]:
        """获取页面指标"""
        conn = self._get_conn()
        row = None
        for candidate in self._path_candidates(path):
            row = conn.execute(
                "SELECT * FROM page_metrics WHERE wiki_path = ?", (candidate,)
            ).fetchone()
            if row:
                break
        if not row:
            return None
        return self._row_to_metrics(row)

    def list_pages(
        self,
        stage: str | None = None,
        status: str | None = None,
        min_quality: float | None = None,
        max_freshness: int | None = None,
    ) -> List[PageMetrics]:
        """列出页面（支持过滤）"""
        conditions = []
        values = []
        if stage:
            conditions.append("knowledge_stage = ?")
            values.append(stage)
        if status:
            conditions.append("status = ?")
            values.append(status)
        if min_quality is not None:
            conditions.append("quality_score >= ?")
            values.append(min_quality)  # type: ignore[arg-type]
        if max_freshness is not None:
            conditions.append("freshness_days <= ?")
            values.append(max_freshness)  # type: ignore[arg-type]

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        conn = self._get_conn()
        rows = conn.execute(
            f"SELECT * FROM page_metrics {where} ORDER BY last_updated DESC",  # nosec B608
            values,
        ).fetchall()
        return [self._row_to_metrics(r) for r in rows]

    def _row_to_metrics(self, row) -> PageMetrics:
        def _json_list(value):
            if not value:
                return []
            if isinstance(value, list):
                return value
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else [str(parsed)]
            except (json.JSONDecodeError, ValueError):
                return [str(value)]

        def _val(name: str, default=None):
            try:
                return row[name]
            except (KeyError, IndexError, TypeError):  # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
                return default

        return PageMetrics(
            wiki_path=_val("wiki_path", row[0]),
            title=_val("title", "") or "",
            page_role=_val("page_role", "knowledge") or "knowledge",
            knowledge_stage=_val("knowledge_stage", "P3") or "P3",
            evidence_level=_val("evidence_level", 1) or 1,
            source_count=_val("source_count", 0) or 0,
            source_refs=_json_list(_val("source_refs", "[]")),
            heat_level=_val("heat_level", "cold") or "cold",
            heat_score=_val("heat_score", 0.0) or 0.0,
            quality_score=_val("quality_score", 0.0) or 0.0,
            quality_level=_val("quality_level", "acceptable") or "acceptable",
            completeness=_val("completeness", 0.0) or 0.0,
            freshness_days=_val("freshness_days", 999) or 999,
            backlink_count=_val("backlink_count", 0) or 0,
            status=_val("status", "draft") or "draft",
            last_updated=_val("last_updated", "") or "",
            last_accessed=_val("last_accessed", "") or "",
            created_at=_val("created_at", "") or "",
            tags=_json_list(_val("tags", "[]")),
        )

    # ---- 质量评估 ----

    def assess_quality(self, path: str, content: str) -> float:
        """评估页面质量并更新"""
        score = quick_quality_score(content)
        level = "poor"
        if score >= 80:
            level = "excellent"
        elif score >= 60:
            level = "good"
        elif score >= 40:
            level = "acceptable"

        self.upsert_page(path, quality_score=round(score, 1), quality_level=level)
        return score

    # ---- 热力更新 ----

    def update_heat(self, path: str, access_type: str = "read"):
        """更新页面热力"""
        metric_path = self._resolve_existing_key(path) or self._canonical_metric_path(path)
        page = self.get_page(metric_path)
        now = _utcnow().isoformat()
        if not page:
            self.upsert_page(
                metric_path,
                heat_level="warm",
                heat_score=1.0,
                last_accessed=now,
                _preserve_last_updated=True,
            )
            return

        # 加分规则
        delta = {"read": 1, "search_hit": 3, "citation": 5, "edit": 2}.get(access_type, 1)
        new_score = min(page.heat_score + delta, 100)

        # 重新计算热力等级
        new_level = compute_heat_level(
            page.last_updated or _utcnow().isoformat(),
            now,
        )

        self.upsert_page(
            metric_path,
            heat_score=new_score,
            heat_level=new_level,
            last_accessed=now,
            _preserve_last_updated=True,
        )

    def _execute_decay_batch(self, conn, batch: list):
        """批量执行衰减更新，失败时逐条 fallback"""
        try:
            conn.executemany(
                "UPDATE page_metrics SET heat_score = ?, heat_level = ? WHERE wiki_path = ?",
                batch,
            )
        except sqlite3.Error:
            logger.warning("Batch decay update failed, falling back to individual", exc_info=True)
            for row in batch:
                try:
                    conn.execute(
                        "UPDATE page_metrics SET heat_score = ?, heat_level = ? WHERE wiki_path = ?",  # noqa: E501
                        row,
                    )
                except (sqlite3.Error, OSError):
                    logger.warning("Failed to decay update for %s", row[2], exc_info=True)

    def decay_all(self, decay_days: int = 15):
        """执行全局热力衰减（batch → individual fallback）"""
        now = _utcnow()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT wiki_path, heat_score, last_updated, last_accessed, tags FROM page_metrics WHERE heat_score > 0"  # noqa: E501
        ).fetchall()

        decayed = 0
        batch = []
        BATCH_SIZE = 100
        for path, score, last_updated, last_accessed, tags in rows:
            try:
                activity = last_accessed or last_updated
                lu = datetime.fromisoformat(activity.replace("Z", "+00:00"))
                if lu.tzinfo is None:
                    lu = lu.replace(tzinfo=timezone.utc)
                days = (now - lu).days
            except (ValueError, TypeError, AttributeError):
                logger.warning(
                    "Failed to parse activity time for %s, defaulting to 0 days",
                    path,
                    exc_info=True,
                )
                days = 0

            page_decay_days = self._decay_days_for(path, tags, decay_days)
            if days >= page_decay_days:
                decay = min(days / page_decay_days, 5)  # 最多减5分
                new_score = max(score - decay, 0)
                new_level = compute_heat_level(last_updated, last_accessed)
                batch.append((new_score, new_level, path))
                if len(batch) >= BATCH_SIZE:
                    self._execute_decay_batch(conn, batch)
                    batch.clear()
                decayed += 1

        if batch:
            self._execute_decay_batch(conn, batch)
        conn.commit()
        return decayed

    def get_pages_by_level(self, level: HeatLevel | str, limit: int = 50) -> List[PageMetrics]:
        """按热力等级获取页面。"""
        level_value = level.value if isinstance(level, HeatLevel) else str(level)
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM page_metrics
               WHERE heat_level = ?
               ORDER BY heat_score DESC, last_updated DESC
               LIMIT ?""",
            (level_value, limit),
        ).fetchall()
        return [self._row_to_metrics(row) for row in rows]

    def get_cold_pages(self, limit: int = 10) -> List[PageMetrics]:
        """获取冷却知识，用于周报/自省报告联动。"""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM page_metrics
               WHERE heat_level = 'cold'
               ORDER BY quality_score ASC, freshness_days DESC, last_updated ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [self._row_to_metrics(row) for row in rows]

    def sync_heat_to_frontmatter(self, page_path: Path) -> bool:
        """将热力数据反写到页面 frontmatter，同步更新原始中文字段。

        修复"两张皮"问题：metrics 系统的状态/阶段/热力数据必须回写到
        用户可见的原始 frontmatter 字段（状态、知识阶段、热度等级等），
        而不是只写带 _metric 后缀的隐藏字段。
        """
        page_path = Path(page_path)
        if is_derived_kg_scan_path(page_path, self.wiki_dir):
            return False
        try:
            rel_path = str(page_path.relative_to(self.wiki_dir))
        except (ValueError, OSError):
            rel_path = str(page_path)
        metrics = self.get_page(rel_path) or self.get_page(str(page_path))
        if not metrics:
            return False
        if not page_path.exists():
            return False

        try:
            content = page_path.read_text(encoding="utf-8")
            fm, body = self._split_frontmatter(content)

            semantic_values = {
                "heat_level": _heat_level_to_display(metrics.heat_level),
                "heat_score": round(metrics.heat_score, 1),
                "quality_score": round(metrics.quality_score, 1),
                "knowledge_stage_metric": metrics.knowledge_stage,
                "status_metric": metrics.status,
                "knowledge_stage": _stage_to_display(metrics.knowledge_stage),
                "status": _status_to_display(metrics.status),
                "source_count": metrics.source_count,
            }
            if all(fm_get(fm, key) == value for key, value in semantic_values.items()):
                return False

            # --- 内部 metrics 字段（仅保留与展示字段不重名的字段） ---
            # Canonical heat/quality keys map to the same Chinese display keys.
            # Writing both forms makes insertion order decide which value wins,
            # so an existing Chinese key can be overwritten by ``hot`` and then
            # rewritten forever on every scan.
            for canonical_key in ("heat_level", "heat_score", "quality_score", "stats_updated"):
                fm.pop(canonical_key, None)
            fm["knowledge_stage_metric"] = metrics.knowledge_stage
            fm["status_metric"] = metrics.status

            # --- 用户可见的原始中文字段（修复两张皮） ---
            fm["热度等级"] = _heat_level_to_display(metrics.heat_level)
            fm["热度分"] = round(metrics.heat_score, 1)
            fm["质量分"] = round(metrics.quality_score, 1)
            fm["知识阶段"] = _stage_to_display(metrics.knowledge_stage)
            fm["状态"] = _status_to_display(metrics.status)
            fm["来源数量"] = metrics.source_count
            fm["统计更新时间"] = _utcnow().strftime("%Y-%m-%d %H:%M")

            fm = to_chinese_frontmatter_preserving_unknown(fm)
            evidence_refs = [f"wiki_metrics:{rel_path}"]
            submit_or_write_markdown_with_decision(
                decision_policy=WIKI_METRICS_MARKDOWN_POLICY,
                decision_facts={
                    "schema_version": "mnemos.wiki_metrics_frontmatter_facts.v1",
                    "wiki_path": rel_path,
                    "metrics": semantic_values,
                    "metrics_db": str(self.db_path),
                },
                decision_task=f"Project Wiki metrics for {rel_path}",
                decision_goal="Keep page metadata aligned with its exact metrics row.",
                decision_created_at=_utcnow().isoformat(),
                wiki_base=self.wiki_dir,
                target_path=page_path,
                content=self._join_frontmatter(fm, body),
                source="wiki_metrics",
                actor="system",
                evidence_refs=evidence_refs,
                proposed_action="update_metrics_frontmatter",
                expected_existing_hash=sha256_text(content),
                metadata={"wiki_path": rel_path},
            )
            return True
        except (OSError, ValueError, TypeError):
            logger.warning("Unexpected error in wiki_metrics.py", exc_info=True)
            return False

    def generate_heat_report(self, write: bool = False, wiki_dir: Optional[str] = None) -> str:
        """生成热力地图 Markdown 报告，可选写入 wiki/99-Reports。"""
        hot = self.get_pages_by_level(HeatLevel.HOT)
        warm = self.get_pages_by_level(HeatLevel.WARM)
        cold = self.get_pages_by_level(HeatLevel.COLD)
        source_page_count = len(hot) + len(warm) + len(cold)
        generated_at = _utcnow()

        lines = [
            "# 热力地图",
            f"生成时间: {generated_at.strftime('%Y-%m-%d %H:%M')}",
            "",
            f"- HOT: {len(hot)}",
            f"- WARM: {len(warm)}",
            f"- COLD: {len(cold)}",
            f"- 来源数据库: `{self.db_path}`",
            f"- 来源页面数: {source_page_count}",
            "",
        ]

        for title, pages in [
            ("## HOT", hot),
            ("## WARM", warm),
            ("## COLD", cold),
        ]:
            lines.extend([title, ""])
            if not pages:
                lines.append("无")
                lines.append("")
                continue
            for page in pages[:20]:
                lines.append(
                    f"- **{page.title or Path(page.wiki_path).stem}** "
                    f"`{page.heat_level}` score={page.heat_score:.1f} quality={page.quality_score:.1f}"  # noqa: E501
                )
            lines.append("")

        report_date = generated_at.strftime("%Y-%m-%d")
        report = write_frontmatter(
            {
                "mnemos_type": "system_report",
                "report_type": "heatmap",
                "report_id": f"heatmap-{report_date}",
                "generated_at": generated_at.isoformat(),
                "source_db": str(self.db_path),
                "source_table": "page_metrics",
                "source_page_count": source_page_count,
                "source_count": source_page_count,
                "sources": [f"sqlite:{self.db_path}#page_metrics"],
                "evidence_level": "multiple" if source_page_count > 1 else "single",
                "knowledge_stage": "P2",
                "status": "active",
                "data_reliability": "db_backed" if source_page_count else "unavailable",
            },
            "\n".join(lines) + "\n",
        )
        if write:
            base = Path(wiki_dir).expanduser() if wiki_dir else self.wiki_dir
            report_dir = base / "99-Reports"
            path = report_dir / f"热力地图-{report_date}.md"
            if source_page_count:
                existing_content = (
                    read_markdown_text(path) if path.is_file() else None
                )
                submit_or_write_markdown_with_decision(
                    decision_policy=WIKI_METRICS_MARKDOWN_POLICY,
                    decision_facts={
                        "schema_version": "mnemos.wiki_heat_report_facts.v1",
                        "metrics_db": str(self.db_path),
                        "report_date": report_date,
                        "source_page_count": source_page_count,
                        "hot_count": len(hot),
                        "warm_count": len(warm),
                        "cold_count": len(cold),
                    },
                    decision_task=f"Write Wiki heat report for {report_date}",
                    decision_goal="Publish the exact database-backed Wiki heat snapshot.",
                    decision_created_at=generated_at.isoformat(),
                    wiki_base=base,
                    target_path=path,
                    content=report,
                    source="wiki_metrics",
                    actor="system",
                    evidence_refs=[f"sqlite:{self.db_path}#page_metrics"],
                    proposed_action="write_heat_map_report",
                    expected_existing_hash=(
                        sha256_text(existing_content)
                        if existing_content is not None
                        else None
                    ),
                    metadata={"source_page_count": source_page_count},
                )
            else:
                logger.warning("热力地图缺少 page_metrics 数据，跳过写入: %s", path)
        return report

    def _decay_days_for(self, path: str, tags_json: str = "[]", default: int = 15) -> int:
        category = self._category_for_path(path, tags_json)
        return self.CATEGORY_DECAY_DAYS.get(category, default)

    def _category_for_path(self, path: str, tags_json: str = "[]") -> str:
        page_path = Path(path)
        if page_path.exists():
            try:
                fm, _ = self._split_frontmatter(page_path.read_text(encoding="utf-8"))
                category = fm.get("category") or fm.get("page_type") or fm_get(fm, "domain")
                if category:
                    return str(category)
            except (ValueError, KeyError, TypeError):
                logger.warning("Unexpected error in wiki_metrics.py", exc_info=True)
        try:
            tags = json.loads(tags_json or "[]")
        except (json.JSONDecodeError, ValueError):
            logger.warning("Unexpected error in wiki_metrics.py", exc_info=True)
            tags = []
        for tag in tags:
            if str(tag).startswith("category:"):
                return str(tag).split(":", 1)[1]
        return ""

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[Dict, str]:
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                if yaml is None:
                    return {}, parts[2].lstrip("\n")
                return yaml.safe_load(parts[1]) or {}, parts[2].lstrip("\n")
        return {}, content

    @staticmethod
    def _join_frontmatter(frontmatter: Dict, body: str) -> str:
        if yaml is not None:
            fm_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        else:
            fm_text = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
        return f"---\n{fm_text}\n---\n{body}"

    # ---- 关系操作 ----

    def add_relation(
        self, from_path: str, to_path: str, relation_type: str = "link", strength: float = 1.0
    ):
        """添加页面关系"""
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO page_relations (from_path, to_path, relation_type, strength)
            VALUES (?, ?, ?, ?)
        """,
            (from_path, to_path, relation_type, strength),
        )
        conn.commit()

        # 更新 backlink_count
        self._update_backlink_count(to_path)

    def _update_backlink_count(self, path: str):
        """更新反向链接计数"""
        conn = self._get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM page_relations WHERE to_path = ?", (path,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE page_metrics SET backlink_count = ? WHERE wiki_path = ?", (count, path)
        )
        conn.commit()

    def get_relations(self, path: str) -> List[Dict]:
        """获取页面关系"""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT to_path, relation_type, strength FROM page_relations
            WHERE from_path = ?
        """,
            (path,),
        ).fetchall()
        return [{"to": r[0], "type": r[1], "strength": r[2]} for r in rows]

    # ---- Curator 专用 ----

    def get_merge_candidates(
        self, min_pages: int = 3, max_freshness: int = WIKI_METRICS_DURATION_BUCKET_MONTH_DAYS
    ) -> List[Dict]:
        """
        获取合并候选（供 curator 使用）

        返回按主题聚合的、适合合并的页面组：
        - P3 页面数量 >= min_pages
        - 或 P2 页面超过7天未更新
        """
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT wiki_path, title, knowledge_stage, freshness_days, quality_score, source_count
            FROM page_metrics
            WHERE knowledge_stage IN ('P2', 'P3') AND status = 'draft'
            ORDER BY title
        """).fetchall()

        # 按主题前缀聚类
        from collections import defaultdict

        topics = defaultdict(list)
        for row in rows:
            path, title, stage, freshness, quality, sc = row
            base = re.sub(r"[-_]?v?\d+$", "", title or path)
            topics[base].append(
                {
                    "path": path,
                    "stage": stage,
                    "freshness": freshness,
                    "quality": quality,
                    "source_count": sc,
                }
            )

        candidates = []
        for topic, pages in topics.items():
            if len(pages) >= min_pages:
                # 检查是否有足够的冷页面
                cold_pages = [p for p in pages if p["freshness"] > max_freshness]
                if len(cold_pages) >= min_pages:
                    candidates.append(
                        {
                            "topic": topic,
                            "total_pages": len(pages),
                            "cold_pages": len(cold_pages),
                            "avg_quality": round(sum(p["quality"] for p in pages) / len(pages), 1),
                            "pages": pages,
                            "suggested_action": "merge_to_p1" if len(pages) >= 5 else "review",
                        }
                    )

        candidates.sort(key=lambda x: x["total_pages"], reverse=True)
        return candidates

    def mark_deprecated(self, path: str, reason: str = "merged"):
        """标记页面为废弃（合并后）"""
        self.upsert_page(path, status="deprecated", tags=[reason])

    def mark_merged(self, path: str, merged_into: str):
        """标记页面已合并"""
        self.upsert_page(
            path,
            status="deprecated",
            tags=json.dumps(["merged", f"into:{merged_into}"], ensure_ascii=False),
        )

    # ---- 统计报告 ----

    def get_summary(self) -> Dict:
        """获取整体统计"""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM page_metrics").fetchone()[0]
        stages = conn.execute("""
            SELECT knowledge_stage, COUNT(*) FROM page_metrics
            GROUP BY knowledge_stage
        """).fetchall()
        statuses = conn.execute("""
            SELECT status, COUNT(*) FROM page_metrics
            GROUP BY status
        """).fetchall()
        avg_quality = conn.execute("""
            SELECT AVG(quality_score) FROM page_metrics WHERE quality_score > 0
        """).fetchone()[0] or 0

        return {
            "total_pages": total,
            "by_stage": {s[0]: s[1] for s in stages},
            "by_status": {s[0]: s[1] for s in statuses},
            "avg_quality": round(avg_quality, 1),
        }

    def generate_report(self) -> str:
        """生成文本报告"""
        summary = self.get_summary()
        lines = [
            "# Wiki Metrics Report",
            f"Generated: {_utcnow().isoformat()}",
            "",
            "## 概览",
            f"- 总页面: {summary['total_pages']}",
            f"- 平均质量: {summary['avg_quality']}/100",
            "",
            "## 知识阶段分布",
        ]
        for stage, count in sorted(summary.get("by_stage", {}).items()):
            lines.append(f"- {stage}: {count} 页")
        lines.append("")
        lines.append("## 状态分布")
        for status, count in sorted(summary.get("by_status", {}).items()):
            lines.append(f"- {status}: {count} 页")

        # 合并候选
        candidates = self.get_merge_candidates(min_pages=3)
        if candidates:
            lines.append("")
            lines.append("## 合并候选")
            for c in candidates[:10]:
                lines.append(
                    f"- **{c['topic']}**: {c['total_pages']} 页 (冷页面: {c['cold_pages']}, 均质: {c['avg_quality']})"  # noqa: E501
                )

        return "\n".join(lines)


# ==================== 6. 便捷函数 ====================

_default_metrics: Optional[WikiMetrics] = None
_metrics_lock = threading.Lock()


def get_default_metrics() -> WikiMetrics:
    """获取全局默认 WikiMetrics 实例"""
    global _default_metrics
    if _default_metrics is None:
        with _metrics_lock:
            if _default_metrics is None:
                _default_metrics = WikiMetrics()
    return _default_metrics


def quick_assess(path: str, content: str, source_count: int = 1) -> Dict:
    """快速评估页面"""
    m = get_default_metrics()
    score = m.assess_quality(path, content)
    stage = compute_knowledge_stage(source_count, "draft")
    level = compute_evidence_level(source_count)
    m.upsert_page(
        path,
        knowledge_stage=stage,
        evidence_level=level,
        source_count=source_count,
        freshness_days=0,
    )
    return {"quality_score": score, "stage": stage, "evidence_level": level}


def write_mnemos_home(wiki_dir: Optional[str] = None, limit: int = 8) -> Optional[Path]:
    """Write a user-facing Obsidian home page for Mnemos activity."""
    wiki = Path(wiki_dir).expanduser() if wiki_dir else get_config().wiki_dir
    wiki.mkdir(parents=True, exist_ok=True)
    metrics = WikiMetrics(wiki_dir=str(wiki))
    summary = metrics.get_summary()
    recent = metrics.list_pages()[:limit]
    hot = sorted(
        metrics.list_pages(),
        key=lambda p: (p.heat_score, p.quality_score),
        reverse=True,
    )[:limit]

    pending_recaps = []
    try:
        from core.app.forced_retrospective import ForcedRetrospective

        forced = ForcedRetrospective()
        pending_recaps = forced.get_pending_system_recaps()[:limit]
    except ImportError:
        logger.debug("dashboard recap load failed", exc_info=True)

    lines = [
        "---",
        "mnemos_type: dashboard",
        "auto_updated: true",
        f"updated: {_utcnow().isoformat()}",
        "---",
        "",
        "# Mnemos Home",
        "",
        "## 系统概览",
        "",
        f"- Wiki metrics 页面数: {summary.get('total_pages', 0)}",
        f"- 平均质量分: {summary.get('avg_quality', 0)}",
        f"- 阶段分布: {json.dumps(summary.get('by_stage', {}), ensure_ascii=False)}",
        f"- 状态分布: {json.dumps(summary.get('by_status', {}), ensure_ascii=False)}",
        "",
        "## 最近更新",
        "",
    ]
    if recent:
        for page in recent:
            lines.append(
                f"- [[{page.wiki_path[:-3] if page.wiki_path.endswith('.md') else page.wiki_path}]]"
                f" · {page.heat_level} · quality {round(page.quality_score, 1)}"
            )
    else:
        lines.append("- 暂无页面 metrics，运行 `mnemos metrics scan`。")

    lines.extend(["", "## 热点知识", ""])
    if hot:
        for page in hot:
            lines.append(
                f"- [[{page.wiki_path[:-3] if page.wiki_path.endswith('.md') else page.wiki_path}]]"
                f" · heat {round(page.heat_score, 1)} · {page.status}"
            )
    else:
        lines.append("- 暂无热点知识。")

    lines.extend(["", "## 待复盘", ""])
    if pending_recaps:
        for recap in pending_recaps:
            target = recap.target_page or "00-Mnemos-Home"
            lines.append(f"- {recap.severity} · [[{target}]] · {recap.topic}")
    else:
        lines.append("- 暂无待复盘事项。")

    lines.extend(
        [
            "",
            "## 使用痕迹",
            "",
            "- Agent 每次任务开始应调用 `preflight_inject`。",
            "- 任务执行中涉及高风险操作应调用 `guard_check`。",
            "- 任务收尾或会话开始应调用 `check_pending_recaps`。",
            "",
        ]
    )

    path = wiki / "00-Mnemos-Home.md"
    existing_content = read_markdown_text(path) if path.is_file() else None
    rendered_content = "\n".join(lines)
    submit_or_write_markdown_with_decision(
        decision_policy=WIKI_METRICS_MARKDOWN_POLICY,
        decision_facts={
            "schema_version": "mnemos.wiki_home_dashboard_facts.v1",
            "metrics_db": str(metrics.db_path),
            "summary": summary,
            "recent_pages": [page.wiki_path for page in recent],
            "hot_pages": [page.wiki_path for page in hot],
            "pending_recap_ids": [recap.task_id for recap in pending_recaps],
        },
        decision_task="Write the Mnemos activity home page",
        decision_goal="Publish the exact current metrics and recap dashboard snapshot.",
        decision_created_at=_utcnow().isoformat(),
        wiki_base=wiki,
        target_path=path,
        content=rendered_content,
        source="wiki_metrics",
        actor="system",
        evidence_refs=[f"sqlite:{metrics.db_path}#page_metrics"],
        proposed_action="write_mnemos_home",
        expected_existing_hash=(
            sha256_text(existing_content) if existing_content is not None else None
        ),
        metadata={"page_count": summary.get("total_pages", 0)},
    )
    return path


# ==================== CLI ====================


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Wiki Metrics CLI")
    parser.add_argument("--assess", help="评估页面质量 (path)")
    parser.add_argument("--content-file", help="页面内容文件路径")
    parser.add_argument("--summary", action="store_true", help="统计摘要")
    parser.add_argument("--report", action="store_true", help="完整报告")
    parser.add_argument("--merge-candidates", action="store_true", help="合并候选")
    parser.add_argument("--decay", action="store_true", help="执行热力衰减")
    parser.add_argument("--get", help="获取页面指标")
    args = parser.parse_args()

    m = get_default_metrics()

    if args.assess:
        content = ""
        if args.content_file:
            content = Path(args.content_file).read_text(encoding="utf-8")
        score = m.assess_quality(args.assess, content)
        logger.info("质量评分: %.1f/100", score)
        return

    if args.get:
        page = m.get_page(args.get)
        if page:
            logger.info("Path: %s", page.wiki_path)
            logger.info("Stage: %s | Quality: %s", page.knowledge_stage, page.quality_score)
            logger.info("Heat: %s (%.1f)", page.heat_level, page.heat_score)
            logger.info("Freshness: %s days", page.freshness_days)
        else:
            logger.info("页面未找到")
        return

    if args.summary:
        logger.info(json.dumps(m.get_summary(), indent=2, ensure_ascii=False))
        return

    if args.report:
        logger.info(m.generate_report())
        return

    if args.merge_candidates:
        candidates = m.get_merge_candidates()
        logger.info(json.dumps(candidates, indent=2, ensure_ascii=False))
        return

    if args.decay:
        m.decay_all()
        logger.info("热力衰减完成")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
