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
    return Path.home() / ".mnemos" / "wiki_state.db"


@dataclass
class Entity:
    """知识实体"""
    uid: str  # 唯一标识（slug）
    name: str
    aliases: List[str] = field(default_factory=list)
    entity_type: str = "concept"  # concept / tool / person / project / pattern
    quality_score: float = 0.5
    confidence: float = 0.5
    status: str = "raw"  # raw → refined → mature
    wiki_page: str = ""
    first_seen: str = ""
    last_updated: str = ""
    source_count: int = 1


class EntityManager:
    """实体管理器"""

    ENTITY_TABLE = """
        CREATE TABLE IF NOT EXISTS entities (
            uid TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            entity_type TEXT DEFAULT 'concept',
            quality_score REAL DEFAULT 0.5,
            confidence REAL DEFAULT 0.5,
            status TEXT DEFAULT 'raw',
            wiki_page TEXT DEFAULT '',
            first_seen TEXT,
            last_updated TEXT,
            source_count INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS entity_aliases (
            alias TEXT PRIMARY KEY,
            entity_uid TEXT NOT NULL,
            FOREIGN KEY (entity_uid) REFERENCES entities(uid)
        );

        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
        CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status);
        CREATE INDEX IF NOT EXISTS idx_entities_quality ON entities(quality_score);
    """

    def __init__(self):
        self._db_path = _get_db_path()
        self._init_db()

    def _init_db(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self._db_path), timeout=5) as conn:
            conn.executescript(self.ENTITY_TABLE)
            conn.commit()

    def ingest_from_wiki(self, wiki_page: Path) -> List[Entity]:
        """从 Wiki 页面提取实体"""
        try:
            content = wiki_page.read_text(encoding="utf-8")
        except Exception:
            return []

        fm = self._parse_frontmatter(content)
        entities = []

        # 从 frontmatter 提取
        kw = fm.get("关键词", {})
        if isinstance(kw, dict):
            for layer in ("核心概念", "工具实体"):
                words = kw.get(layer, [])
                if isinstance(words, list):
                    for word in words:
                        if isinstance(word, str) and len(word) >= 2:
                            entity_type = "tool" if layer == "工具实体" else "concept"
                            entities.append(self._upsert_entity(
                                name=word, entity_type=entity_type,
                                wiki_page=str(wiki_page),
                            ))

        # 从 [[链接]] 提取
        links = re.findall(r'\[\[([^\]]+)\]\]', content)
        for link in links:
            link = link.split("|")[0].strip()
            if len(link) >= 2:
                entities.append(self._upsert_entity(
                    name=link, entity_type="concept",
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
            pass

        # 再查名称
        return self.get_entity_by_name(name)

    def get_entity(self, uid: str) -> Optional[Entity]:
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                cursor = conn.execute(
                    "SELECT uid, name, entity_type, quality_score, confidence, "
                    "status, wiki_page, first_seen, last_updated, source_count "
                    "FROM entities WHERE uid = ?", (uid,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                aliases = self._get_aliases(uid)
                return Entity(
                    uid=row[0], name=row[1], aliases=aliases,
                    entity_type=row[2], quality_score=row[3], confidence=row[4],
                    status=row[5], wiki_page=row[6], first_seen=row[7],
                    last_updated=row[8], source_count=row[9],
                )
        except Exception:
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
            wiki_page=wiki_page, first_seen=datetime.now().isoformat(),
            last_updated=datetime.now().isoformat(), source_count=1,
        )
        self._save_entity(entity)
        return entity

    def _save_entity(self, entity: Entity):
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO entities
                       (uid, name, entity_type, quality_score, confidence,
                        status, wiki_page, first_seen, last_updated, source_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (entity.uid, entity.name, entity.entity_type,
                     entity.quality_score, entity.confidence, entity.status,
                     entity.wiki_page, entity.first_seen, entity.last_updated,
                     entity.source_count),
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
