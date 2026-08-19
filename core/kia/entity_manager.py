# -*- coding: utf-8 -*-
"""
EntityManager — 实体管理器

职责：
- 从 Wiki 页面提取实体
- AdaptiveScorerV2 质量评分
- 贝叶斯质量更新
- 别名解析（K8s → Kubernetes）
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

from core.config import ConfigProvider, get_config
from core.db_utils import sqlite_conn

# Constants extracted from magic numbers
ENTITY_MANAGER_GET_ENTITY_ROW = 7
logger = logging.getLogger(__name__)


def _get_db_path() -> Path:
    # 统一存储到 knowledge_graph.db，与 RelationManager / KnowledgeGraph 共享
    return Path(get_config().database_dir) / "knowledge_graph.db"


@dataclass
class Entity:
    """知识实体（对齐蓝图 §3.1）"""

    uid: str  # 唯一标识（slug）
    name: str
    entity_type: str = "concept"  # page / concept / technology / project / person
    source_page: str = ""  # 来源 wiki 页面路径
    quality_score: float = 0.5
    confidence: float = 0.5
    temporal_scope: str = "stable"  # permanent / stable / version-bound / contextual
    version_info: Optional[str] = None  # 版本号（如 Python 3.12）
    status: str = "active"  # active / deprecated / merged
    visit_count: int = 0  # 被访问次数（暗知识反馈）
    tags: Set[str] = field(default_factory=set)
    aliases: List[str] = field(default_factory=list)
    first_seen: str = ""
    last_updated: str = ""
    source_count: int = 1


class EntityManager:
    """实体管理器"""

    # 硬限制：防止实体数量无限增长
    MAX_ENTITIES_PER_PAGE = 10
    GLOBAL_MAX_ENTITIES = 1000
    LOW_QUALITY_THRESHOLD = 0.3

    ENTITY_TABLE = """
        CREATE TABLE IF NOT EXISTS entities (
            uid TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            entity_type TEXT DEFAULT 'concept',
            source_page TEXT DEFAULT '',
            quality_score REAL DEFAULT 0.5,
            confidence REAL DEFAULT 0.5,
            temporal_scope TEXT DEFAULT 'stable',
            version_info TEXT,
            status TEXT DEFAULT 'active',
            visit_count INTEGER DEFAULT 0,
            tags TEXT DEFAULT '[]',
            first_seen TEXT,
            last_updated TEXT,
            source_count INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS entity_aliases (
            alias TEXT PRIMARY KEY,
            entity_uid TEXT NOT NULL,
            FOREIGN KEY (entity_uid) REFERENCES entities(uid)
        );

        CREATE TABLE IF NOT EXISTS entity_sources (
            entity_uid TEXT NOT NULL,
            source_page TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (entity_uid, source_page),
            FOREIGN KEY (entity_uid) REFERENCES entities(uid)
        );
    """

    ENTITY_INDEXES = """
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
        CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status);
        CREATE INDEX IF NOT EXISTS idx_entities_quality ON entities(quality_score);
        CREATE INDEX IF NOT EXISTS idx_entities_visit ON entities(visit_count);
    """

    MIGRATIONS = [
        "ALTER TABLE entities ADD COLUMN source_page TEXT DEFAULT '';",
        "ALTER TABLE entities ADD COLUMN confidence REAL DEFAULT 0.5;",
        "ALTER TABLE entities ADD COLUMN temporal_scope TEXT DEFAULT 'stable';",
        "ALTER TABLE entities ADD COLUMN version_info TEXT;",
        "ALTER TABLE entities ADD COLUMN status TEXT DEFAULT 'active';",
        "ALTER TABLE entities ADD COLUMN visit_count INTEGER DEFAULT 0;",
        "ALTER TABLE entities ADD COLUMN tags TEXT DEFAULT '[]';",
        "ALTER TABLE entities ADD COLUMN first_seen TEXT;",
        "ALTER TABLE entities ADD COLUMN last_updated TEXT;",
        "ALTER TABLE entities ADD COLUMN source_count INTEGER DEFAULT 1;",
    ]

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        config: Optional[ConfigProvider] = None,
        *,
        initialize: bool = True,
        read_only: bool = False,
    ):
        if db_path:
            self._db_path = Path(db_path).expanduser()
        elif config is not None:
            self._db_path = Path(config.database_dir) / "knowledge_graph.db"
        else:
            self._db_path = _get_db_path()
        self.read_only = bool(read_only)
        if initialize:
            if self.read_only:
                raise ValueError("read-only EntityManager cannot initialize schema")
            self._init_db()

    def _read_target(self) -> tuple[str, bool]:
        if self.read_only:
            if not self._db_path.is_file():
                raise FileNotFoundError(self._db_path)
            return f"file:{self._db_path.resolve(strict=True)}?mode=ro", True
        return str(self._db_path), False

    def _assert_writable(self) -> None:
        """Reject every mutation through a projection-replay manager."""

        if self.read_only:
            raise PermissionError("read-only EntityManager cannot mutate canonical state")

    @staticmethod
    def _migration_column(migration: str) -> str:
        match = re.search(r"ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)", migration, re.I)
        return match.group(1) if match else ""

    def _init_db(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        from core.cognitive.material_effect_schema import (
            initialize_material_effect_schema,
        )
        from .relation_evidence_schema import (
            validate_existing_relation_evidence_schema,
        )

        with sqlite_conn(str(self._db_path), timeout=5) as conn:
            validate_existing_relation_evidence_schema(conn)
            initialize_material_effect_schema(conn)
            conn.executescript(self.ENTITY_TABLE)
            # Explicit bootstrap ensures the current entity columns.
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(entities)")}
            for migration in self.MIGRATIONS:
                col = self._migration_column(migration)
                if col and col not in existing_cols:
                    conn.execute(migration)
                    existing_cols.add(col)
            # 索引在迁移后创建，避免旧表缺少列导致失败
            conn.executescript(self.ENTITY_INDEXES)
            now = datetime.now().isoformat()
            conn.execute(
                """INSERT OR IGNORE INTO entity_sources
                   (entity_uid, source_page, first_seen, last_seen)
                   SELECT uid, source_page, COALESCE(first_seen, ?), COALESCE(last_updated, ?)
                   FROM entities WHERE source_page != ''""",
                (now, now),
            )
            conn.commit()

    # 实体质量过滤：排除切片伪实体和停用词
    _ENTITY_STOP_WORDS = {
        "的",
        "了",
        "是",
        "在",
        "与",
        "及",
        "或",
        "为",
        "有",
        "和",
        "中",
        "上",
        "下",
        "前",
        "后",
        "内",
        "外",
        "间",
    }
    # 中文虚词出现在中间时，极可能是句子切片
    _ZH_FUNCTION_WORDS_MIDDLE = {"与", "在", "过"}
    # 明显是句子切片的起止模式
    _BAD_STARTS = ("在", "被", "把", "将", "对", "从")
    _BAD_ENDS = ("过", "的", "了", "是", "有")

    @classmethod
    def _is_valid_entity_name(cls, name: str) -> bool:
        """校验实体名称是否有效（排除切片伪实体）"""
        if not isinstance(name, str):
            return False
        name = name.strip()
        if len(name) < 2 or len(name) > 50:
            return False
        # 排除纯数字/纯标点
        if not any(c.isalpha() or "\u4e00" <= c <= "\u9fff" for c in name):
            return False
        # 排除明显切片：包含不完整的连接词且总长度过短
        if name in cls._ENTITY_STOP_WORDS:
            return False
        # 排除句子切片模式
        if name.startswith(cls._BAD_STARTS) or name.endswith(cls._BAD_ENDS):
            return False
        # 虚词出现在中间且前后都有内容 → 句子切片
        for fw in cls._ZH_FUNCTION_WORDS_MIDDLE:
            if fw in name[1:-1]:
                return False
        # 要求至少包含一个完整词汇（中文字数>=2 或 英文单词>=2字母）
        zh_chars = sum(1 for c in name if "\u4e00" <= c <= "\u9fff")
        en_words = [w for w in re.split(r"[^a-zA-Z0-9]", name) if len(w) >= 2]
        if zh_chars < 2 and not en_words:
            return False
        return True

    def ingest_from_wiki(self, wiki_page: Path, content: str | None = None) -> List[Entity]:
        """从 Wiki 页面提取实体

        Args:
            wiki_page: Wiki 页面路径
            content: 可选，已读取的页面内容（避免重复 I/O）
        """
        self._assert_writable()
        if content is None:
            try:
                content = wiki_page.read_text(encoding="utf-8")
            except (OSError, IOError):
                logging.getLogger(__name__).warning(
                    "Caught unexpected error at entity_manager.py", exc_info=True
                )
                return []

        names = self.extract_entity_names(content)
        entities = [
            self._upsert_entity(
                name=name,
                entity_type="concept",
                wiki_page=str(wiki_page),
            )
            for name in names
        ]

        # 全局实体数超过上限时清理低质量实体
        self._cleanup_low_quality_entities_if_needed()

        return entities

    @classmethod
    def extract_entity_names(cls, content: str) -> List[str]:
        """Return the exact bounded entity-name sequence without writing state."""

        fm = cls._parse_frontmatter(content)
        keywords = fm.get("关键词", {})
        candidates: List[str] = []
        if isinstance(keywords, dict):
            for layer in ("核心概念", "工具实体"):
                words = keywords.get(layer, [])
                if isinstance(words, list):
                    candidates.extend(str(word) for word in words)
        elif isinstance(keywords, list):
            candidates.extend(str(word) for word in keywords)

        links = re.findall(r"\[\[([^\]]+)\]\]", content)
        candidates.extend(
            (link.split("|")[0].strip().split("/")[-1])
            for link in links
        )
        names: List[str] = []
        for candidate in candidates:
            if len(names) >= cls.MAX_ENTITIES_PER_PAGE:
                break
            if cls._is_valid_entity_name(candidate):
                names.append(candidate)
        return names

    def update_quality(
        self, entity_uid: str, _feedback_expected: float, feedback_actual: float
    ) -> None:
        """贝叶斯质量更新（EWMA）"""
        self._assert_writable()
        entity = self.get_entity(entity_uid)
        if not entity:
            return

        alpha = 0.1
        calibrated_feedback = self._calibrated_feedback_score(
            _feedback_expected, feedback_actual
        )
        # EWMA 更新 quality_score
        entity.quality_score = (
            alpha * calibrated_feedback + (1 - alpha) * entity.quality_score
        )
        # 置信度随反馈增加
        entity.confidence = min(1.0, entity.confidence + 0.05)
        entity.last_updated = datetime.now().isoformat()

        # 状态迁移
        if entity.status == "raw" and entity.source_count >= 3 and entity.confidence >= 0.6:
            entity.status = "refined"
        elif entity.status == "refined" and entity.source_count >= 5 and entity.confidence >= 0.8:
            entity.status = "mature"

        self._save_entity(entity)

    @staticmethod
    def _calibrated_feedback_score(
        feedback_expected: float, feedback_actual: float
    ) -> float:
        try:
            expected = float(feedback_expected)
            actual = float(feedback_actual)
        except (TypeError, ValueError):
            return 0.0
        if expected > 0:
            actual = actual / expected
        return max(0.0, min(1.0, actual))

    def resolve_alias(self, name: str) -> Optional[Entity]:
        """别名解析"""
        # 先查别名表
        try:
            target, uri = self._read_target()
            with sqlite_conn(target, timeout=5, uri=uri, wal=not uri) as conn:
                cursor = conn.execute(
                    "SELECT entity_uid FROM entity_aliases WHERE alias = ?",
                    (name.lower(),),
                )
                row = cursor.fetchone()
                if row:
                    return self.get_entity(row[0])
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at entity_manager.py", exc_info=True
            )

        # 再查名称
        return self.get_entity_by_name(name)

    def get_entity(self, uid: str) -> Optional[Entity]:
        try:
            target, uri = self._read_target()
            with sqlite_conn(target, timeout=5, uri=uri, wal=not uri) as conn:
                cursor = conn.execute(
                    "SELECT uid, name, entity_type, source_page, quality_score, confidence, "
                    "temporal_scope, version_info, status, visit_count, tags, "
                    "first_seen, last_updated, source_count "
                    "FROM entities WHERE uid = ?",
                    (uid,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                aliases = self._get_aliases(uid)
                tags_raw = row[10] or "[]"
                try:
                    tags = set(json.loads(tags_raw)) if tags_raw else set()
                except json.JSONDecodeError:
                    tags = set()
                return Entity(
                    uid=row[0],
                    name=row[1],
                    entity_type=row[2],
                    source_page=row[3] or "",
                    quality_score=row[4],
                    confidence=row[5],
                    temporal_scope=row[6] or "stable",
                    version_info=row[ENTITY_MANAGER_GET_ENTITY_ROW],
                    status=row[8] or "active",
                    visit_count=row[9] or 0,
                    tags=tags,
                    aliases=aliases,
                    first_seen=row[11],
                    last_updated=row[12],
                    source_count=int(row[13]) if row[13] is not None else 1,
                )
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.warning("读取实体失败: %s", uid)
            return None

    def get_entity_by_name(self, name: str) -> Optional[Entity]:
        uid = self._slugify(name)
        return self.get_entity(uid)

    def add_alias(self, entity_uid: str, alias: str) -> None:
        """添加别名"""
        self._assert_writable()
        try:
            with sqlite_conn(str(self._db_path), timeout=5) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO entity_aliases (alias, entity_uid) VALUES (?, ?)",
                    (alias.lower(), entity_uid),
                )
                conn.commit()
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).warning("Caught unexpected error", exc_info=True)

    def get_all_entities(
        self, entity_type: str | None = None, min_quality: float = 0.0
    ) -> List[Entity]:
        """获取所有实体"""
        try:
            target, uri = self._read_target()
            with sqlite_conn(target, timeout=5, uri=uri, wal=not uri) as conn:
                query = "SELECT uid FROM entities WHERE quality_score >= ?"
                params = [min_quality]
                if entity_type:
                    query += " AND entity_type = ?"
                    params.append(entity_type)  # type: ignore[arg-type]
                query += " ORDER BY quality_score DESC, confidence DESC, uid ASC"
                cursor = conn.execute(query, params)
                entity_ids = [row[0] for row in cursor]
            entities = [self.get_entity(uid) for uid in entity_ids]
            return [entity for entity in entities if entity is not None]
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at entity_manager.py", exc_info=True
            )
            return []

    def get_entity_sources(self, entity_uid: str) -> List[str]:
        """Return distinct Wiki provenance refs for one entity in first-seen order."""

        try:
            target, uri = self._read_target()
            with sqlite_conn(target, timeout=5, uri=uri, wal=not uri) as conn:
                rows = conn.execute(
                    """SELECT source_page FROM entity_sources
                       WHERE entity_uid=? ORDER BY first_seen, source_page""",
                    (entity_uid,),
                ).fetchall()
            return [str(row[0]) for row in rows if str(row[0] or "")]
        except (sqlite3.Error, OSError):
            logger.warning("读取实体来源失败: %s", entity_uid, exc_info=True)
            return []

    # ---- 公共方法 ----

    def add_entity(
        self, name: str, entity_type: str = "concept", wiki_page: str = ""
    ) -> Optional[Entity]:
        """按名称插入或更新实体（供外部调用，如 Charon、KGEventHandler）。

        Returns:
            创建/更新后的 Entity，若名称不合法则返回 None。
        """
        self._assert_writable()
        if not self._is_valid_entity_name(name):
            return None
        return self._upsert_entity(name, entity_type=entity_type, wiki_page=wiki_page)

    # ---- 内部方法 ----

    def _upsert_entity(
        self, name: str, entity_type: str = "concept", wiki_page: str = ""
    ) -> Entity:
        """插入或更新实体"""
        self._assert_writable()
        uid = self._slugify(name)
        existing = self.get_entity(uid)
        if existing:
            if wiki_page:
                source_count, source_added = self._record_entity_source(uid, wiki_page)
                if (
                    not source_added
                    and existing.source_count == source_count
                    and existing.source_page
                ):
                    return existing
                existing.source_count = source_count
                if existing.status == "source_missing":
                    existing.status = "active"
            else:
                existing.source_count += 1
            existing.last_updated = datetime.now().isoformat()
            if wiki_page and not existing.source_page:
                existing.source_page = wiki_page
            self._save_entity(existing)
            return existing

        entity = Entity(
            uid=uid,
            name=name,
            entity_type=entity_type,
            source_page=wiki_page,
            first_seen=datetime.now().isoformat(),
            last_updated=datetime.now().isoformat(),
            source_count=1,
        )
        self._save_entity(entity)
        if wiki_page:
            entity.source_count, _ = self._record_entity_source(uid, wiki_page)
            self._save_entity(entity)
        return entity

    def _record_entity_source(self, entity_uid: str, source_page: str) -> tuple[int, bool]:
        """Record one distinct Wiki source and return ``(count, was_added)``."""

        self._assert_writable()
        now = datetime.now().isoformat()
        with sqlite_conn(str(self._db_path), timeout=5) as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO entity_sources
                   (entity_uid, source_page, first_seen, last_seen)
                   VALUES (?, ?, ?, ?)""",
                (entity_uid, str(source_page), now, now),
            )
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM entity_sources WHERE entity_uid=?",
                    (entity_uid,),
                ).fetchone()[0]
            )
            conn.commit()
        return max(1, count), bool(cursor.rowcount)

    def _save_entity(self, entity: Entity):
        self._assert_writable()
        try:
            with sqlite_conn(str(self._db_path), timeout=5) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO entities
                       (uid, name, entity_type, source_page, quality_score, confidence,
                        temporal_scope, version_info, status, visit_count, tags,
                        first_seen, last_updated, source_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entity.uid,
                        entity.name,
                        entity.entity_type,
                        entity.source_page,
                        entity.quality_score,
                        entity.confidence,
                        entity.temporal_scope,
                        entity.version_info,
                        entity.status,
                        entity.visit_count,
                        json.dumps(sorted(entity.tags)),
                        entity.first_seen,
                        entity.last_updated,
                        entity.source_count,
                    ),
                )
                # 保存别名
                for alias in entity.aliases:
                    conn.execute(
                        "INSERT OR REPLACE INTO entity_aliases (alias, entity_uid) VALUES (?, ?)",
                        (alias.lower(), entity.uid),
                    )
                conn.commit()
        except (sqlite3.Error, OSError) as e:
            logger.warning("保存实体失败: %s", e)

    def _cleanup_low_quality_entities_if_needed(self) -> None:
        """当全局实体数超过上限时，删除低质量且长期未访问的实体。"""
        self._assert_writable()
        try:
            with sqlite_conn(str(self._db_path), timeout=5) as conn:
                total = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
                if total <= self.GLOBAL_MAX_ENTITIES:
                    return
                # 删除质量分低且长期未更新的实体，保留最近 90 天内更新过的
                cutoff = (datetime.now() - timedelta(days=90)).isoformat()
                cursor = conn.execute(
                    """DELETE FROM entities
                       WHERE quality_score < ?
                         AND last_updated < ?
                         AND visit_count < 3""",
                    (self.LOW_QUALITY_THRESHOLD, cutoff),
                )
                deleted = cursor.rowcount
                if deleted:
                    logger.info(
                        "[EntityManager] 全局实体数 %s 超过上限 %s，已清理 %s 个低质量实体",
                        total,
                        self.GLOBAL_MAX_ENTITIES,
                        deleted,
                    )
                conn.commit()
        except (sqlite3.Error, OSError) as e:
            logger.warning("[EntityManager] 清理低质量实体失败: %s", e)

    def _get_aliases(self, uid: str) -> List[str]:
        try:
            target, uri = self._read_target()
            with sqlite_conn(target, timeout=5, uri=uri, wal=not uri) as conn:
                cursor = conn.execute(
                    "SELECT alias FROM entity_aliases WHERE entity_uid = ?",
                    (uid,),
                )
                return [row[0] for row in cursor]
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at entity_manager.py", exc_info=True
            )
            return []

    @staticmethod
    def _slugify(name: str) -> str:
        """将名称转为 slug（uid）"""
        slug = name.lower().strip()
        slug = re.sub(r"[^\w一-龥-]", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug[:64] if slug else "unknown"

    @staticmethod
    def _parse_frontmatter(content: str) -> Dict:
        if not content.startswith("---"):
            return {}
        end = content.find("---", 3)
        if end == -1:
            return {}
        raw = content[3:end].strip()
        try:
            import yaml

            fm = yaml.safe_load(raw) or {}
            if isinstance(fm, dict):
                return fm
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            yaml.error.YAMLError,
        ):
            logger.debug("yaml frontmatter parse failed, fallback to simple parser", exc_info=True)

        fm = {}
        for line in raw.split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip()
                if val.startswith("["):
                    try:
                        val = json.loads(val)
                    except json.JSONDecodeError:
                        logger.warning(
                            "[entity_manager] json.JSONDecodeError suppressed", exc_info=True
                        )
                fm[key] = val
        return fm
