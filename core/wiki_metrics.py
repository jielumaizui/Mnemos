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
import logging
logger = logging.getLogger(__name__)

import sqlite3
import json
import re
import hashlib
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import List, Dict, Optional
from dataclasses import dataclass, field

from core.config import get_config
from core.frontmatter import fm_get, to_chinese_frontmatter

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


# ==================== 1. 枚举 ====================

class KnowledgeStage(Enum):
    """知识固化阶段（P0-P3，对应项目已有架构）"""
    P0 = "P0"   # Wiki Page: 成熟页面（source_count >= 6, verified）
    P1 = "P1"   # Merged Topic: 已合并（status == 'merged'）
    P2 = "P2"   # Refined: 多次积累（source_count > 1）
    P3 = "P3"   # Raw: 首次创建（source_count <= 1）


class HeatLevel(Enum):
    """简化热力层级"""
    COLD = "cold"      # 30天无更新/访问
    WARM = "warm"      # 7-30天
    HOT = "hot"        # 7天内有更新/访问


class QualityLevel(Enum):
    """质量等级"""
    EXCELLENT = "excellent"   # >= 80分
    GOOD = "good"             # 60-79分
    ACCEPTABLE = "acceptable" # 40-59分
    POOR = "poor"             # < 40分


# ==================== 2. _LazyPath ====================

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


DB_PATH = _LazyPath("data_dir", "wiki_metrics.db")
WIKI_DIR = _LazyPath("wiki_dir")


# ==================== 3. 工具函数 ====================

def _utcnow() -> datetime:
    """返回带时区的当前 UTC 时间"""
    return datetime.now(timezone.utc)


def compute_evidence_level(source_count: int) -> int:
    """根据来源数计算证据等级 (1-4)"""
    if source_count >= 6:
        return 4
    elif source_count >= 4:
        return 3
    elif source_count >= 2:
        return 2
    return 1


def compute_knowledge_stage(source_count: int, status: str = "draft") -> str:
    """计算知识固化阶段 (P0-P3)"""
    if status == "verified" and source_count >= 6:
        return "P0"
    if status == "merged":
        return "P1"
    if source_count > 1:
        return "P2"
    return "P3"


def compute_heat_level(last_updated: str, last_accessed: str = None) -> str:
    """根据时间计算热力等级"""
    now = _utcnow()
    try:
        lu = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
        if lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
        days_since_update = (now - lu).days
    except Exception:
        logger.warning(f"Unexpected error in wiki_metrics.py", exc_info=True)
        days_since_update = 999

    if last_accessed:
        try:
            la = datetime.fromisoformat(last_accessed.replace('Z', '+00:00'))
            if la.tzinfo is None:
                la = la.replace(tzinfo=timezone.utc)
            days_since_access = (now - la).days
        except Exception:
            logger.warning(f"Unexpected error in wiki_metrics.py", exc_info=True)
            days_since_access = 999
        days = min(days_since_update, days_since_access)
    else:
        days = days_since_update

    if days <= 7:
        return "hot"
    elif days <= 30:
        return "warm"
    return "cold"


def hash_query(query: str) -> str:
    """计算查询的归一化哈希"""
    normalized = re.sub(r'\s+', ' ', query.lower().strip())
    return hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:12]


def quick_quality_score(content: str) -> float:
    """快速质量评分 (0-100)

    简化的四维度评估：
    - 信息密度：有效字符 / 总长度
    - 结构化：标题、列表、代码块数量
    - 链接质量：内部/外部链接数量
    - 丰富度：字数、实体提及
    """
    if not content or len(content) < 10:
        return 0.0

    # 1. 密度（去除markdown语法后的实际内容密度）
    clean = re.sub(r'[#*`\[\]\(\)\-_>]', '', content)
    density = min(len(clean.strip()) / max(len(content), 1), 1.0) * 25

    # 2. 结构化（标题、列表、代码块）
    headers = len(re.findall(r'^#{1,6}\s', content, re.MULTILINE))
    lists = len(re.findall(r'^[\s]*[-*+\d]\.', content, re.MULTILINE))
    code_blocks = len(re.findall(r'```', content)) // 2
    structure = min((headers * 3 + lists * 1 + code_blocks * 5) / 20, 1.0) * 25

    # 3. 链接质量
    internal_links = len(re.findall(r'\[\[.*?\]\]', content))
    external_links = len(re.findall(r'\[.*?\]\(https?://', content))
    links = min((internal_links * 3 + external_links * 2) / 10, 1.0) * 25

    # 4. 丰富度
    word_count = len(content.split())
    richness = min(word_count / 500, 1.0) * 25

    return min(density + structure + links + richness, 100.0)


# ==================== 4. 数据类 ====================

@dataclass
class PageMetrics:
    """页面度量数据"""
    wiki_path: str
    title: str = ""
    knowledge_stage: str = "P3"
    evidence_level: int = 1
    source_count: int = 0
    source_memos: List[str] = field(default_factory=list)
    heat_level: str = "cold"
    heat_score: float = 0.0
    quality_score: float = 0.0
    quality_level: str = "acceptable"
    completeness: float = 0.0      # 0-1
    freshness_days: int = 999      # 距最后更新天数
    backlink_count: int = 0
    status: str = "draft"          # draft / merged / verified / deprecated
    last_updated: str = ""
    created_at: str = ""
    tags: List[str] = field(default_factory=list)


# ==================== 5. WikiMetrics ====================

class WikiMetrics:
    """
    Wiki 度量中心

    单一数据库存储所有 wiki 页面的质量、热力和阶段信息。
    """

    CATEGORY_DECAY_DAYS = {
        "technology": 7,
        "methodology": 30,
        "practice": 60,
    }

    def __init__(self, db_path: Optional[str] = None, wiki_dir: Optional[str] = None):
        if db_path is not None:
            self._db_path = Path(db_path)
        else:
            self._db_path = None  # 使用 _LazyPath
        self._wiki_dir = Path(wiki_dir).expanduser() if wiki_dir else None
        self._local = threading.local()
        self._init_db()

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

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            db = self.db_path
            db.parent.mkdir(parents=True, exist_ok=True)
            self._local.conn = sqlite3.connect(str(db), timeout=10, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self):
        """初始化数据库"""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS page_metrics (
                wiki_path TEXT PRIMARY KEY,
                title TEXT,
                knowledge_stage TEXT DEFAULT 'P3',
                evidence_level INTEGER DEFAULT 1,
                source_count INTEGER DEFAULT 0,
                source_memos TEXT DEFAULT '[]',
                heat_level TEXT DEFAULT 'cold',
                heat_score REAL DEFAULT 0.0,
                quality_score REAL DEFAULT 0.0,
                quality_level TEXT DEFAULT 'acceptable',
                completeness REAL DEFAULT 0.0,
                freshness_days INTEGER DEFAULT 999,
                backlink_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'draft',
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
        conn.commit()

    # ---- 页面操作 ----

    def upsert_page(self, path: str, **kwargs):
        """插入或更新页面指标"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM page_metrics WHERE wiki_path = ?",
            (path,)
        ).fetchone()

        if row:
            # 更新：只更新提供的字段
            allowed = [
                "title", "knowledge_stage", "evidence_level", "source_count",
                "source_memos", "heat_level", "heat_score", "quality_score",
                "quality_level", "completeness", "freshness_days",
                "backlink_count", "status", "last_updated", "tags"
            ]
            updates = []
            values = []
            for k, v in kwargs.items():
                if k in allowed:
                    if k in ("source_memos", "tags") and isinstance(v, list):
                        v = json.dumps(v, ensure_ascii=False)
                    updates.append(f"{k} = ?")
                    values.append(v)
            if updates:
                if "last_updated" not in kwargs:
                    updates.append("last_updated = ?")
                    values.append(_utcnow().isoformat())
                values.append(path)
                conn.execute(
                    f"UPDATE page_metrics SET {', '.join(updates)} WHERE wiki_path = ?",
                    values
                )
                conn.commit()
        else:
            # 插入新记录
            defaults = {
                "title": "", "knowledge_stage": "P3", "evidence_level": 1,
                "source_count": 0, "source_memos": "[]",
                "heat_level": "cold", "heat_score": 0.0,
                "quality_score": 0.0, "quality_level": "acceptable",
                "completeness": 0.0, "freshness_days": 999,
                "backlink_count": 0, "status": "draft",
                "tags": "[]", "last_updated": _utcnow().isoformat(),
                "created_at": _utcnow().isoformat(),
            }
            defaults.update(kwargs)
            for k in ("source_memos", "tags"):
                if isinstance(defaults.get(k), list):
                    defaults[k] = json.dumps(defaults[k], ensure_ascii=False)

            conn.execute("""
                INSERT INTO page_metrics
                (wiki_path, title, knowledge_stage, evidence_level, source_count,
                 source_memos, heat_level, heat_score, quality_score, quality_level,
                 completeness, freshness_days, backlink_count, status,
                 last_updated, created_at, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                path, defaults["title"], defaults["knowledge_stage"],
                defaults["evidence_level"], defaults["source_count"],
                defaults["source_memos"], defaults["heat_level"],
                defaults["heat_score"], defaults["quality_score"],
                defaults["quality_level"], defaults["completeness"],
                defaults["freshness_days"], defaults["backlink_count"],
                defaults["status"], defaults["last_updated"],
                defaults["created_at"], defaults["tags"]
            ))
            conn.commit()

    def scan_all_pages(self) -> Dict[str, int]:
        """全量扫描 Wiki 目录，为所有页面创建/更新 metrics"""
        import yaml
        wiki = self.wiki_dir
        if not wiki.exists():
            return {"total": 0, "inserted": 0, "updated": 0}

        inserted = 0
        updated = 0
        seen_paths = set()
        for md_file in wiki.rglob("*.md"):
            try:
                rel_path = str(md_file.relative_to(wiki))
                seen_paths.add(rel_path)
                content = md_file.read_text(encoding="utf-8", errors="ignore")
                title = md_file.stem
                status = "draft"
                tags = []
                knowledge_stage = "P3"

                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            fm = yaml.safe_load(parts[1]) or {}
                            title = fm.get("名称", fm.get("title", fm.get("Name", title)))
                            status_map = {"草稿": "draft", "已验证": "verified", "待审": "review", "废弃": "archived"}
                            status = status_map.get(fm.get("状态", ""), "draft")
                            tags = fm.get("tags", [])
                            if isinstance(tags, str):
                                tags = [tags]
                            elif not isinstance(tags, list):
                                tags = []
                            stage_map = {"原始": "P3", "初筛": "P2", "已整理": "P2", "已验证": "P0", "成熟": "P0"}
                            knowledge_stage = stage_map.get(fm.get("知识阶段", ""), "P3")
                        except Exception:
                            pass

                quality_score = quick_quality_score(content)
                if quality_score >= 80:
                    quality_level = "excellent"
                elif quality_score >= 60:
                    quality_level = "good"
                elif quality_score >= 40:
                    quality_level = "acceptable"
                else:
                    quality_level = "poor"

                stat = md_file.stat()
                last_updated = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                freshness_days = max(0, (_utcnow() - datetime.fromtimestamp(stat.st_mtime, timezone.utc)).days)
                heat_level = compute_heat_level(last_updated)
                heat_score = {"hot": 3.0, "warm": 1.0, "cold": 0.0}.get(heat_level, 0.0)
                completeness = min(1.0, quality_score / 100)
                if status == "draft" and quality_score >= 60:
                    status = "active"
                if knowledge_stage == "P3":
                    if quality_score >= 80:
                        knowledge_stage = "P1"
                    elif quality_score >= 60:
                        knowledge_stage = "P2"

                row = self._get_conn().execute(
                    "SELECT 1 FROM page_metrics WHERE wiki_path = ?", (rel_path,)
                ).fetchone()
                payload = {
                    "title": title,
                    "status": status,
                    "tags": tags,
                    "knowledge_stage": knowledge_stage,
                    "quality_score": round(quality_score, 1),
                    "quality_level": quality_level,
                    "freshness_days": freshness_days,
                    "heat_level": heat_level,
                    "heat_score": heat_score,
                    "completeness": round(completeness, 2),
                    "last_updated": last_updated,
                }
                if row:
                    self.upsert_page(rel_path, **payload)
                    updated += 1
                else:
                    self.upsert_page(rel_path, **payload)
                    inserted += 1
                try:
                    self.sync_heat_to_frontmatter(md_file)
                except Exception:
                    logger.debug("frontmatter sync failed for %s", md_file, exc_info=True)
            except Exception:
                continue

        deleted = 0
        if seen_paths:
            conn = self._get_conn()
            placeholders = ",".join("?" for _ in seen_paths)
            cursor = conn.execute(
                f"DELETE FROM page_metrics WHERE wiki_path NOT IN ({placeholders})",
                tuple(seen_paths),
            )
            deleted = cursor.rowcount
            conn.commit()

        return {"total": inserted + updated, "inserted": inserted, "updated": updated, "deleted": deleted}

    def get_page(self, path: str) -> Optional[PageMetrics]:
        """获取页面指标"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM page_metrics WHERE wiki_path = ?",
            (path,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_metrics(row)

    def list_pages(self, stage: str = None, status: str = None,
                   min_quality: float = None, max_freshness: int = None) -> List[PageMetrics]:
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
            values.append(min_quality)
        if max_freshness is not None:
            conditions.append("freshness_days <= ?")
            values.append(max_freshness)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        conn = self._get_conn()
        rows = conn.execute(
            f"SELECT * FROM page_metrics {where} ORDER BY last_updated DESC",
            values
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
            except Exception:
                return [str(value)]

        return PageMetrics(
            wiki_path=row[0],
            title=row[1] or "",
            knowledge_stage=row[2] or "P3",
            evidence_level=row[3] or 1,
            source_count=row[4] or 0,
            source_memos=_json_list(row[5]),
            heat_level=row[6] or "cold",
            heat_score=row[7] or 0.0,
            quality_score=row[8] or 0.0,
            quality_level=row[9] or "acceptable",
            completeness=row[10] or 0.0,
            freshness_days=row[11] or 999,
            backlink_count=row[12] or 0,
            status=row[13] or "draft",
            last_updated=row[14] or "",
            created_at=row[15] or "",
            tags=_json_list(row[16]),
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
        page = self.get_page(path)
        if not page:
            self.upsert_page(path, heat_level="warm", heat_score=1.0)
            return

        # 加分规则
        delta = {"read": 1, "search_hit": 3, "citation": 5, "edit": 2}.get(access_type, 1)
        new_score = min(page.heat_score + delta, 100)

        # 重新计算热力等级
        new_level = compute_heat_level(
            page.last_updated or _utcnow().isoformat(),
            _utcnow().isoformat()
        )

        self.upsert_page(path, heat_score=new_score, heat_level=new_level)

    def decay_all(self, decay_days: int = 15):
        """执行全局热力衰减"""
        now = _utcnow()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT wiki_path, heat_score, last_updated, tags FROM page_metrics WHERE heat_score > 0"
        ).fetchall()

        for path, score, last_updated, tags in rows:
            try:
                lu = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                if lu.tzinfo is None:
                    lu = lu.replace(tzinfo=timezone.utc)
                days = (now - lu).days
            except Exception:
                logger.warning(f"Unexpected error in wiki_metrics.py", exc_info=True)
                days = 0

            page_decay_days = self._decay_days_for(path, tags, decay_days)
            if days >= page_decay_days:
                decay = min(days / page_decay_days, 5)  # 最多减5分
                new_score = max(score - decay, 0)
                new_level = compute_heat_level(last_updated)
                conn.execute(
                    "UPDATE page_metrics SET heat_score = ?, heat_level = ? WHERE wiki_path = ?",
                    (new_score, new_level, path)
                )
        conn.commit()

    def get_pages_by_level(self, level: HeatLevel | str, limit: int = 50) -> List[PageMetrics]:
        """按热力等级获取页面。"""
        level_value = level.value if isinstance(level, HeatLevel) else str(level)
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM page_metrics
               WHERE heat_level = ?
               ORDER BY heat_score DESC, last_updated DESC
               LIMIT ?""",
            (level_value, limit)
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
            (limit,)
        ).fetchall()
        return [self._row_to_metrics(row) for row in rows]

    def sync_heat_to_frontmatter(self, page_path: Path) -> bool:
        """将热力数据反写到页面 frontmatter，供 Obsidian Graph View 使用。"""
        page_path = Path(page_path)
        try:
            rel_path = str(page_path.relative_to(self.wiki_dir))
        except Exception:
            rel_path = str(page_path)
        metrics = self.get_page(rel_path) or self.get_page(str(page_path))
        if not metrics:
            return False
        if not page_path.exists():
            return False

        try:
            content = page_path.read_text(encoding="utf-8")
            fm, body = self._split_frontmatter(content)
            fm["heat_level"] = metrics.heat_level
            fm["heat_score"] = round(metrics.heat_score, 1)
            fm["quality_score"] = round(metrics.quality_score, 1)
            fm["knowledge_stage_metric"] = metrics.knowledge_stage
            fm["status_metric"] = metrics.status
            fm["stats_updated"] = _utcnow().isoformat()
            fm = to_chinese_frontmatter(fm)
            page_path.write_text(self._join_frontmatter(fm, body), encoding="utf-8")
            return True
        except Exception:
            logger.warning(f"Unexpected error in wiki_metrics.py", exc_info=True)
            return False

    def generate_heat_report(self, write: bool = False, wiki_dir: Optional[str] = None) -> str:
        """生成热力地图 Markdown 报告，可选写入 wiki/99-Reports。"""
        hot = self.get_pages_by_level(HeatLevel.HOT)
        warm = self.get_pages_by_level(HeatLevel.WARM)
        cold = self.get_pages_by_level(HeatLevel.COLD)

        lines = [
            "# 热力地图",
            f"生成时间: {_utcnow().strftime('%Y-%m-%d %H:%M')}",
            "",
            f"- HOT: {len(hot)}",
            f"- WARM: {len(warm)}",
            f"- COLD: {len(cold)}",
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
                    f"`{page.heat_level}` score={page.heat_score:.1f} quality={page.quality_score:.1f}"
                )
            lines.append("")

        report = "\n".join(lines)
        if write:
            base = Path(wiki_dir).expanduser() if wiki_dir else self.wiki_dir
            report_dir = base / "99-Reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            path = report_dir / f"热力地图-{_utcnow().strftime('%Y-%m-%d')}.md"
            path.write_text(report, encoding="utf-8")
        return report

    def _decay_days_for(self, path: str, tags_json: str = "[]", default: int = 15) -> int:
        category = self._category_for_path(path, tags_json)
        return self.CATEGORY_DECAY_DAYS.get(category, default)

    def _category_for_path(self, path: str, tags_json: str = "[]") -> str:
        page_path = Path(path)
        if page_path.exists():
            try:
                fm, _ = self._split_frontmatter(page_path.read_text(encoding="utf-8"))
                category = (
                    fm.get("category")
                    or fm.get("page_type")
                    or fm_get(fm, "domain")
                )
                if category:
                    return str(category)
            except Exception:
                logger.warning(f"Unexpected error in wiki_metrics.py", exc_info=True)
                pass
        try:
            tags = json.loads(tags_json or "[]")
        except Exception:
            logger.warning(f"Unexpected error in wiki_metrics.py", exc_info=True)
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

    def add_relation(self, from_path: str, to_path: str, relation_type: str = "link", strength: float = 1.0):
        """添加页面关系"""
        conn = self._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO page_relations (from_path, to_path, relation_type, strength)
            VALUES (?, ?, ?, ?)
        """, (from_path, to_path, relation_type, strength))
        conn.commit()

        # 更新 backlink_count
        self._update_backlink_count(to_path)

    def _update_backlink_count(self, path: str):
        """更新反向链接计数"""
        conn = self._get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM page_relations WHERE to_path = ?",
            (path,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE page_metrics SET backlink_count = ? WHERE wiki_path = ?",
            (count, path)
        )
        conn.commit()

    def get_relations(self, path: str) -> List[Dict]:
        """获取页面关系"""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT to_path, relation_type, strength FROM page_relations
            WHERE from_path = ?
        """, (path,)).fetchall()
        return [{"to": r[0], "type": r[1], "strength": r[2]} for r in rows]

    # ---- Curator 专用 ----

    def get_merge_candidates(self, min_pages: int = 3, max_freshness: int = 30) -> List[Dict]:
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
            base = re.sub(r'[-_]?v?\d+$', '', title or path)
            topics[base].append({
                "path": path,
                "stage": stage,
                "freshness": freshness,
                "quality": quality,
                "source_count": sc,
            })

        candidates = []
        for topic, pages in topics.items():
            if len(pages) >= min_pages:
                # 检查是否有足够的冷页面
                cold_pages = [p for p in pages if p["freshness"] > max_freshness]
                if len(cold_pages) >= min_pages:
                    candidates.append({
                        "topic": topic,
                        "total_pages": len(pages),
                        "cold_pages": len(cold_pages),
                        "avg_quality": round(sum(p["quality"] for p in pages) / len(pages), 1),
                        "pages": pages,
                        "suggested_action": "merge_to_p1" if len(pages) >= 5 else "review",
                    })

        candidates.sort(key=lambda x: x["total_pages"], reverse=True)
        return candidates

    def mark_deprecated(self, path: str, reason: str = "merged"):
        """标记页面为废弃（合并后）"""
        self.upsert_page(path, status="deprecated", tags=[reason])

    def mark_merged(self, path: str, merged_into: str):
        """标记页面已合并"""
        self.upsert_page(
            path, status="deprecated",
            tags=json.dumps(["merged", f"into:{merged_into}"], ensure_ascii=False)
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
            f"# Wiki Metrics Report",
            f"Generated: {_utcnow().isoformat()}",
            f"",
            f"## 概览",
            f"- 总页面: {summary['total_pages']}",
            f"- 平均质量: {summary['avg_quality']}/100",
            f"",
            f"## 知识阶段分布",
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
                lines.append(f"- **{c['topic']}**: {c['total_pages']} 页 (冷页面: {c['cold_pages']}, 均质: {c['avg_quality']})")

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
    except Exception:
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

    lines.extend([
        "",
        "## 使用痕迹",
        "",
        "- Agent 每次任务开始应调用 `preflight_inject`。",
        "- 任务执行中涉及高风险操作应调用 `guard_check`。",
        "- 任务收尾或会话开始应调用 `check_pending_recaps`。",
        "",
    ])

    path = wiki / "00-Mnemos-Home.md"
    path.write_text("\n".join(lines), encoding="utf-8")
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
        logger.info(f"质量评分: {score:.1f}/100")
        return

    if args.get:
        page = m.get_page(args.get)
        if page:
            logger.info(f"Path: {page.wiki_path}")
            logger.info(f"Stage: {page.knowledge_stage} | Quality: {page.quality_score}")
            logger.info(f"Heat: {page.heat_level} ({page.heat_score:.1f})")
            logger.info(f"Freshness: {page.freshness_days} days")
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
