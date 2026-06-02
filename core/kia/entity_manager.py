# -*- coding: utf-8 -*-
"""
EntityManager — 实体管理器

职责：
- 从 Wiki 页面提取实体
- AdaptiveScorer 质量评分
- 贝叶斯质量更新
- 别名解析（K8s → Kubernetes）
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from core.config import get_config
logger = logging.getLogger(__name__)



def _get_db_path() -> Path:
    # 统一存储到 knowledge_graph.db，与 RelationManager / KnowledgeGraph 共享
    from core.config import get_config
    return Path(get_config().data_dir) / "knowledge_graph.db"


@dataclass
class Entity:
    """知识实体（对齐蓝图 §3.1）"""
    uid: str                          # 唯一标识（slug）
    name: str
    entity_type: str = "concept"      # page / concept / technology / project / person
    source_page: str = ""             # 来源 wiki 页面路径（wiki_page 的别名）
    quality_score: float = 0.5
    confidence: float = 0.5
    temporal_scope: str = "stable"    # permanent / stable / version-bound / contextual
    version_info: Optional[str] = None  # 版本号（如 Python 3.12）
    status: str = "active"            # active / deprecated / merged
    visit_count: int = 0              # 被访问次数（暗知识反馈）
    tags: Set[str] = field(default_factory=set)
    aliases: List[str] = field(default_factory=list)
    first_seen: str = ""
    last_updated: str = ""
    source_count: int = 1

    @property
    def wiki_page(self) -> str:
        """向后兼容：wiki_page 是 source_page 的别名"""
        return self.source_page

    @wiki_page.setter
    def wiki_page(self, value: str) -> None:
        self.source_page = value


class EntityManager:
    """实体管理器"""

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
    """

    ENTITY_INDEXES = """
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
        CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status);
        CREATE INDEX IF NOT EXISTS idx_entities_quality ON entities(quality_score);
        CREATE INDEX IF NOT EXISTS idx_entities_visit ON entities(visit_count);
    """

    MIGRATIONS = [
        "ALTER TABLE entities ADD COLUMN source_page TEXT DEFAULT '';",
        "ALTER TABLE entities ADD COLUMN temporal_scope TEXT DEFAULT 'stable';",
        "ALTER TABLE entities ADD COLUMN version_info TEXT;",
        "ALTER TABLE entities ADD COLUMN status TEXT DEFAULT 'active';",
        "ALTER TABLE entities ADD COLUMN visit_count INTEGER DEFAULT 0;",
        "ALTER TABLE entities ADD COLUMN tags TEXT DEFAULT '[]';",
    ]

    def __init__(self):
        self._db_path = _get_db_path()
        self._init_db()

    def _init_db(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self._db_path), timeout=5) as conn:
            conn.executescript(self.ENTITY_TABLE)
            # 迁移：兼容旧表结构（先迁移，再建索引）
            for mig in self.MIGRATIONS:
                try:
                    conn.execute(mig)
                except sqlite3.OperationalError:
                    pass  # 列已存在
            # 索引在迁移后创建，避免旧表缺少列导致失败
            conn.executescript(self.ENTITY_INDEXES)
            conn.commit()

    # 实体质量过滤：排除切片伪实体和停用词
    _ENTITY_STOP_WORDS = {
        "的", "了", "是", "在", "与", "及", "或", "为", "有", "和",
        "中", "上", "下", "前", "后", "内", "外", "间",
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
        if not any(c.isalpha() or '\u4e00' <= c <= '\u9fff' for c in name):
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
        zh_chars = sum(1 for c in name if '\u4e00' <= c <= '\u9fff')
        en_words = [w for w in re.split(r'[^a-zA-Z0-9]', name) if len(w) >= 2]
        if zh_chars < 2 and not en_words:
            return False
        return True

    def ingest_from_wiki(self, wiki_page: Path) -> List[Entity]:
        """从 Wiki 页面提取实体"""
        try:
            content = wiki_page.read_text(encoding="utf-8")
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at entity_manager.py", exc_info=True)
            return []

        fm = self._parse_frontmatter(content)
        entities = []

        # 从 frontmatter 提取（支持 dict 层级和 list 两种格式）
        kw = fm.get("关键词", {})
        keyword_words = []
        if isinstance(kw, dict):
            for layer in ("核心概念", "工具实体"):
                words = kw.get(layer, [])
                if isinstance(words, list):
                    keyword_words.extend(words)
        elif isinstance(kw, list):
            keyword_words = kw

        for word in keyword_words:
            if self._is_valid_entity_name(word):
                entities.append(self._upsert_entity(
                    name=word, entity_type="concept",
                    wiki_page=str(wiki_page),
                ))

        # 从 [[链接]] 提取（清洗路径前缀）
        links = re.findall(r'\[\[([^\]]+)\]\]', content)
        for link in links:
            link = link.split("|")[0].strip()
            # 去掉路径前缀如 00-Inbox/、03-Tech/ 等
            clean_link = link.split("/")[-1] if "/" in link else link
            if self._is_valid_entity_name(clean_link):
                entities.append(self._upsert_entity(
                    name=clean_link, entity_type="concept",
                    wiki_page=str(wiki_page),
                ))

        return entities

    def update_quality(self, entity_uid: str, feedback_expected: float,
                       feedback_actual: float) -> None:
        """贝叶斯质量更新（EWMA）"""
        entity = self.get_entity(entity_uid)
        if not entity:
            return

        alpha = 0.1
        # EWMA 更新 quality_score
        entity.quality_score = alpha * feedback_actual + (1 - alpha) * entity.quality_score
        # 置信度随反馈增加
        entity.confidence = min(1.0, entity.confidence + 0.05)
        entity.last_updated = datetime.now().isoformat()

        # 状态迁移
        if entity.status == "raw" and entity.source_count >= 3 and entity.confidence >= 0.6:
            entity.status = "refined"
        elif entity.status == "refined" and entity.source_count >= 5 and entity.confidence >= 0.8:
            entity.status = "mature"

        self._save_entity(entity)

    def resolve_alias(self, name: str) -> Optional[Entity]:
        """别名解析"""
        # 先查别名表
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                cursor = conn.execute(
                    "SELECT entity_uid FROM entity_aliases WHERE alias = ?",
                    (name.lower(),),
                )
                row = cursor.fetchone()
                if row:
                    return self.get_entity(row[0])
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at entity_manager.py", exc_info=True)
            pass

        # 再查名称
        return self.get_entity_by_name(name)

    def get_entity(self, uid: str) -> Optional[Entity]:
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                cursor = conn.execute(
                    "SELECT uid, name, entity_type, source_page, quality_score, confidence, "
                    "temporal_scope, version_info, status, visit_count, tags, "
                    "first_seen, last_updated, source_count "
                    "FROM entities WHERE uid = ?", (uid,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                aliases = self._get_aliases(uid)
                tags_raw = row[10] or '[]'
                try:
                    tags = set(json.loads(tags_raw)) if tags_raw else set()
                except json.JSONDecodeError:
                    tags = set()
                return Entity(
                    uid=row[0], name=row[1], entity_type=row[2],
                    source_page=row[3] or "",
                    quality_score=row[4], confidence=row[5],
                    temporal_scope=row[6] or "stable",
                    version_info=row[7],
                    status=row[8] or "active",
                    visit_count=row[9] or 0,
                    tags=tags,
                    aliases=aliases,
                    first_seen=row[11],
                    last_updated=row[12],
                    source_count=row[13] or 1,
                )
        except Exception:
            logger.warning(f"读取实体失败: {uid}", exc_info=True)
            return None

    def get_entity_by_name(self, name: str) -> Optional[Entity]:
        uid = self._slugify(name)
        return self.get_entity(uid)

    def add_alias(self, entity_uid: str, alias: str) -> None:
        """添加别名"""
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO entity_aliases (alias, entity_uid) VALUES (?, ?)",
                    (alias.lower(), entity_uid),
                )
                conn.commit()
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
    def get_all_entities(self, entity_type: str = None,
                         min_quality: float = 0.0) -> List[Entity]:
        """获取所有实体"""
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                query = "SELECT uid FROM entities WHERE quality_score >= ?"
                params = [min_quality]
                if entity_type:
                    query += " AND entity_type = ?"
                    params.append(entity_type)
                query += " ORDER BY quality_score DESC"
                cursor = conn.execute(query, params)
                return [self.get_entity(row[0]) for row in cursor if self.get_entity(row[0])]
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at entity_manager.py", exc_info=True)
            return []

    # ---- 内部方法 ----

    def _upsert_entity(self, name: str, entity_type: str = "concept",
                       wiki_page: str = "") -> Entity:
        """插入或更新实体"""
        uid = self._slugify(name)
        existing = self.get_entity(uid)
        if existing:
            existing.source_count += 1
            existing.last_updated = datetime.now().isoformat()
            if wiki_page and not existing.wiki_page:
                existing.wiki_page = wiki_page
            self._save_entity(existing)
            return existing

        entity = Entity(
            uid=uid, name=name, entity_type=entity_type,
            source_page=wiki_page, first_seen=datetime.now().isoformat(),
            last_updated=datetime.now().isoformat(), source_count=1,
        )
        self._save_entity(entity)
        return entity

    def _save_entity(self, entity: Entity):
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO entities
                       (uid, name, entity_type, source_page, quality_score, confidence,
                        temporal_scope, version_info, status, visit_count, tags,
                        first_seen, last_updated, source_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (entity.uid, entity.name, entity.entity_type,
                     entity.source_page, entity.quality_score, entity.confidence,
                     entity.temporal_scope, entity.version_info, entity.status,
                     entity.visit_count, json.dumps(sorted(entity.tags)),
                     entity.first_seen, entity.last_updated, entity.source_count),
                )
                # 保存别名
                for alias in entity.aliases:
                    conn.execute(
                        "INSERT OR REPLACE INTO entity_aliases (alias, entity_uid) VALUES (?, ?)",
                        (alias.lower(), entity.uid),
                    )
                conn.commit()
        except Exception as e:
            logger.warning(f"保存实体失败: {e}")

    def _get_aliases(self, uid: str) -> List[str]:
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                cursor = conn.execute(
                    "SELECT alias FROM entity_aliases WHERE entity_uid = ?", (uid,),
                )
                return [row[0] for row in cursor]
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at entity_manager.py", exc_info=True)
            return []

    @staticmethod
    def _slugify(name: str) -> str:
        """将名称转为 slug（uid）"""
        slug = name.lower().strip()
        slug = re.sub(r'[^\w一-龥-]', '-', slug)
        slug = re.sub(r'-+', '-', slug).strip('-')
        return slug[:64] if slug else "unknown"

    @staticmethod
    def _parse_frontmatter(content: str) -> Dict:
        if not content.startswith("---"):
            return {}
        end = content.find("---", 3)
        if end == -1:
            return {}
        fm = {}
        for line in content[3:end].strip().split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip()
                if val.startswith("["):
                    try:
                        val = json.loads(val)
                    except json.JSONDecodeError:
                        pass
                fm[key] = val
        return fm
