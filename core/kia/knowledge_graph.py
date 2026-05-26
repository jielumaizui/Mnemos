# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Knowledge Graph Manager - 知识图谱管理器

基于 SQLite 存储 Wiki 页面之间的语义关系，支持：
- CRUD 操作（自动维护对称关系的双向一致性）
- 自动关系发现（关键词重叠、链接解析、反模式关联）
- 路径查找（A 到 B 的知识路径）
- 冲突检测（矛盾关系环）
- 导出 Obsidian 格式（Mermaid / Dataview）

设计原则：
- 与蒸馏流程解耦，后置增强
- 支持增量更新，新页面入库时自动发现关系
- 关系带置信度，低置信度关系可人工审核
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone

from core.config import get_config
from .relation_schema import (
    Relation, RelationType, RelationEvidence,
    RELATION_META, suggest_relation_type,
)


# ========== _LazyPath（避免模块级副作用）==========

def _get_wiki_dir():
    """Lazy-load wiki directory to avoid side effects at import time."""
    return get_config().wiki_dir


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


# 模块级路径常量：首次访问时才解析
DB_PATH = _LazyPath("data_dir", "knowledge_graph.db")
WIKI_DIR = _LazyPath("wiki_dir")


# ========== 数据库 Schema ==========

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    strength REAL DEFAULT 0.5,
    confidence REAL DEFAULT 0.5,
    source_method TEXT DEFAULT 'auto',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, target, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_rel_source ON relations(source);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relations(target);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relations(relation_type);
CREATE INDEX IF NOT EXISTS idx_rel_confidence ON relations(confidence);

CREATE TABLE IF NOT EXISTS relation_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,
    content TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (relation_id) REFERENCES relations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS relation_stats (
    node TEXT PRIMARY KEY,
    in_degree INTEGER DEFAULT 0,
    out_degree INTEGER DEFAULT 0,
    hub_score REAL DEFAULT 0.0,
    last_calculated TEXT
);
"""


@dataclass
class PathNode:
    """路径节点"""
    page: str
    relation_type: str
    strength: float


@dataclass
class KnowledgePath:
    """知识路径"""
    nodes: List[PathNode]
    total_strength: float = 0.0
    length: int = 0


class KnowledgeGraph:
    """知识图谱门面 — 委托给 EntityManager + RelationManager

    保留原有 CRUD + 发现 + 路径 + 导出接口，
    新增：entity_manager / relation_manager / context_query / event_handler 子系统。
    """

    def __init__(self, db_path: str = None, wiki_base: str = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.db_path = Path(db_path) if db_path else Path(DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        # 子系统（延迟初始化）
        self._entity_manager = None
        self._relation_manager = None
        self._context_query = None
        self._event_handler = None

    @property
    def entity_manager(self):
        if self._entity_manager is None:
            from .entity_manager import EntityManager
            self._entity_manager = EntityManager()
        return self._entity_manager

    @property
    def relation_manager(self):
        if self._relation_manager is None:
            from .relation_manager import RelationManager
            self._relation_manager = RelationManager(str(self.db_path))
        return self._relation_manager

    @property
    def context_query(self):
        if self._context_query is None:
            from .context_query import ContextAwareQuery
            self._context_query = ContextAwareQuery(self.wiki_base)
        return self._context_query

    @property
    def event_handler(self):
        if self._event_handler is None:
            from .kg_event_handler import KGEventHandler
            self._event_handler = KGEventHandler()
        return self._event_handler

    def _init_db(self):
        """初始化数据库"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.executescript(DB_SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ========== CRUD ==========

    def add_relation(self, relation: Relation) -> bool:
        """
        添加关系

        自动处理对称关系：如果关系是对称的，同时添加反向关系
        """
        try:
            with self._conn() as conn:
                # 插入主关系
                cursor = conn.execute(
                    """INSERT OR REPLACE INTO relations
                       (source, target, relation_type, strength, confidence, source_method, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (relation.source, relation.target, relation.relation_type.value,
                     relation.strength, relation.confidence, relation.source_method,
                     datetime.now(timezone.utc).isoformat()[:19])
                )
                rel_id = cursor.lastrowid

                # 插入证据
                for ev in (relation.evidence or []):
                    conn.execute(
                        """INSERT INTO relation_evidence (relation_id, evidence_type, content)
                           VALUES (?, ?, ?)""",
                        (rel_id, ev.evidence_type, ev.content)
                    )

                # 对称关系：自动添加反向
                if relation.is_symmetric and relation.source != relation.target:
                    conn.execute(
                        """INSERT OR REPLACE INTO relations
                           (source, target, relation_type, strength, confidence, source_method, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (relation.target, relation.source, relation.relation_type.value,
                         relation.strength, relation.confidence, relation.source_method,
                         datetime.now(timezone.utc).isoformat()[:19])
                    )

                conn.commit()
                return True
        except sqlite3.Error:
            return False

    def remove_relation(self, source: str, target: str,
                        relation_type: RelationType = None) -> bool:
        """删除关系"""
        try:
            with self._conn() as conn:
                if relation_type:
                    conn.execute(
                        "DELETE FROM relations WHERE source=? AND target=? AND relation_type=?",
                        (source, target, relation_type.value)
                    )
                    # 对称关系同时删反向
                    meta = RELATION_META.get(relation_type, {})
                    if meta.get("symmetric"):
                        conn.execute(
                            "DELETE FROM relations WHERE source=? AND target=? AND relation_type=?",
                            (target, source, relation_type.value)
                        )
                else:
                    conn.execute(
                        "DELETE FROM relations WHERE source=? AND target=?",
                        (source, target)
                    )
                conn.commit()
                return True
        except sqlite3.Error:
            return False

    def get_relations(self, page: str,
                      relation_type: RelationType = None,
                      min_confidence: float = 0.0) -> List[Relation]:
        """获取某页面的出边关系"""
        query = """SELECT r.*, e.evidence_type, e.content
                   FROM relations r
                   LEFT JOIN relation_evidence e ON r.id = e.relation_id
                   WHERE r.source = ? AND r.confidence >= ?"""
        params = [page, min_confidence]

        if relation_type:
            query += " AND r.relation_type = ?"
            params.append(relation_type.value)

        query += " ORDER BY r.strength DESC, r.confidence DESC"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()

        return self._rows_to_relations(rows)

    def get_incoming_relations(self, page: str,
                                min_confidence: float = 0.0) -> List[Relation]:
        """获取指向某页面的入边关系"""
        query = """SELECT r.*, e.evidence_type, e.content
                   FROM relations r
                   LEFT JOIN relation_evidence e ON r.id = e.relation_id
                   WHERE r.target = ? AND r.confidence >= ?
                   ORDER BY r.strength DESC, r.confidence DESC"""

        with self._conn() as conn:
            rows = conn.execute(query, (page, min_confidence)).fetchall()

        return self._rows_to_relations(rows)

    def get_all_relations(self, page: str,
                          min_confidence: float = 0.0) -> Tuple[List[Relation], List[Relation]]:
        """获取页面的所有关系（出边 + 入边）"""
        return (
            self.get_relations(page, min_confidence=min_confidence),
            self.get_incoming_relations(page, min_confidence=min_confidence),
        )

    def _rows_to_relations(self, rows: List[sqlite3.Row]) -> List[Relation]:
        """将数据库行转换为 Relation 对象"""
        relations_map: Dict[str, Relation] = {}

        for row in rows:
            rel_key = f"{row['source']}:{row['target']}:{row['relation_type']}"
            if rel_key not in relations_map:
                relations_map[rel_key] = Relation(
                    source=row["source"],
                    target=row["target"],
                    relation_type=RelationType(row["relation_type"]),
                    strength=row["strength"],
                    confidence=row["confidence"],
                    source_method=row["source_method"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    evidence=[],
                )
            if row["evidence_type"]:
                relations_map[rel_key].evidence.append(RelationEvidence(
                    evidence_type=row["evidence_type"],
                    content=row["content"] or "",
                ))

        return list(relations_map.values())

    # ========== 自动关系发现 ==========

    def discover_relations(self, new_page_path: Path,
                           existing_pages: List[Path] = None) -> List[Relation]:
        """
        自动发现新页面与现有页面的关系

        发现策略：
        1. 解析 [[链接]] → REFERENCES 关系
        2. frontmatter 关键词重叠 → SIMILAR_TO / PART_OF
        3. 反模式文本匹配 → CONTRADICTS
        4. 标题关键词匹配 → BUILDS_ON / SPECIALIZES
        """
        discovered = []

        if not new_page_path.exists():
            return discovered

        new_content = new_page_path.read_text(encoding="utf-8")
        new_meta = self._extract_frontmatter(new_content)
        new_title = self._extract_title(new_content) or new_page_path.stem
        new_keywords = self._extract_all_keywords(new_meta)
        new_links = self._extract_wiki_links(new_content)

        # 获取现有页面列表
        if existing_pages is None:
            inbox = self.wiki_base / "00-Inbox"
            existing_pages = list(inbox.glob("*.md")) if inbox.exists() else []

        # 1. [[链接]] 解析 → REFERENCES
        for link_target in new_links:
            discovered.append(Relation(
                source=str(new_page_path),
                target=link_target,
                relation_type=RelationType.REFERENCES,
                strength=0.9,
                confidence=0.95,
                source_method="link_parse",
                evidence=[RelationEvidence(
                    evidence_type="wiki_link",
                    content=f"页面中显式链接到 [[{link_target}]]",
                )],
            ))

        # 2-4. 与现有页面逐一比对
        for existing_path in existing_pages:
            if existing_path == new_page_path:
                continue

            try:
                existing_content = existing_path.read_text(encoding="utf-8")
                existing_meta = self._extract_frontmatter(existing_content)
                existing_title = self._extract_title(existing_content) or existing_path.stem
                existing_keywords = self._extract_all_keywords(existing_meta)
            except Exception:
                continue

            # 关键词重叠
            overlap = set(new_keywords) & set(existing_keywords)
            if overlap:
                overlap_ratio = len(overlap) / max(len(new_keywords), len(existing_keywords), 1)
                if overlap_ratio >= 0.3:
                    discovered.append(Relation(
                        source=str(new_page_path),
                        target=str(existing_path),
                        relation_type=RelationType.SIMILAR_TO,
                        strength=min(overlap_ratio + 0.3, 0.9),
                        confidence=overlap_ratio,
                        source_method="keyword_overlap",
                        evidence=[RelationEvidence(
                            evidence_type="keyword_overlap",
                            content=f"共同关键词: {', '.join(list(overlap)[:5])}",
                        )],
                    ))

            # 反模式文本匹配 → CONTRADICTS
            new_anti = new_meta.get("反模式", []) or []
            existing_title_parts = existing_title.lower().split()
            for anti in new_anti:
                anti_lower = anti.lower()
                if any(part in anti_lower for part in existing_title_parts if len(part) > 2):
                    discovered.append(Relation(
                        source=str(new_page_path),
                        target=str(existing_path),
                        relation_type=RelationType.CONTRADICTS,
                        strength=0.7,
                        confidence=0.6,
                        source_method="anti_pattern_match",
                        evidence=[RelationEvidence(
                            evidence_type="anti_pattern_quote",
                            content=f"反模式提及: {anti[:100]}",
                        )],
                    ))

            # 标题包含关系
            if existing_title.lower() in new_title.lower() and len(existing_title) > 5:
                discovered.append(Relation(
                    source=str(new_page_path),
                    target=str(existing_path),
                    relation_type=RelationType.SPECIALIZES,
                    strength=0.6,
                    confidence=0.5,
                    source_method="title_containment",
                    evidence=[RelationEvidence(
                        evidence_type="title_match",
                        content=f"标题包含: '{existing_title}' in '{new_title}'",
                    )],
                ))
            elif new_title.lower() in existing_title.lower() and len(new_title) > 5:
                discovered.append(Relation(
                    source=str(new_page_path),
                    target=str(existing_path),
                    relation_type=RelationType.GENERALIZES,
                    strength=0.6,
                    confidence=0.5,
                    source_method="title_containment",
                    evidence=[RelationEvidence(
                        evidence_type="title_match",
                        content=f"标题被包含: '{new_title}' in '{existing_title}'",
                    )],
                ))

        # 去重（按 source+target+type）
        seen = set()
        unique = []
        for rel in discovered:
            key = (rel.source, rel.target, rel.relation_type.value)
            if key not in seen:
                seen.add(key)
                unique.append(rel)

        return unique

    def apply_discovered(self, relations: List[Relation],
                         min_confidence: float = 0.5) -> int:
        """将发现的关系写入数据库（过滤低置信度）"""
        count = 0
        for rel in relations:
            if rel.confidence >= min_confidence:
                if self.add_relation(rel):
                    count += 1
        return count

    # ========== 路径与簇 ==========

    def find_path(self, from_page: str, to_page: str,
                  max_depth: int = 4,
                  min_strength: float = 0.3) -> Optional[KnowledgePath]:
        """
        查找从 A 到 B 的知识路径（BFS + 加权）

        Returns:
            最短且强度最高的路径，或 None
        """
        if from_page == to_page:
            return KnowledgePath(nodes=[], total_strength=1.0, length=0)

        # BFS，优先队列按累计强度排序
        from heapq import heappush, heappop

        visited = set()
        queue = [(-1.0, from_page, [])]  # (-strength, current_page, path)

        while queue:
            neg_strength, current, path = heappop(queue)
            strength = -neg_strength

            if current in visited:
                continue
            visited.add(current)

            if len(path) >= max_depth:
                continue

            # 获取当前页面的出边
            rels = self.get_relations(current, min_confidence=min_strength)
            for rel in rels:
                if rel.strength < min_strength:
                    continue

                new_path = path + [PathNode(
                    page=rel.target,
                    relation_type=rel.relation_type.value,
                    strength=rel.strength,
                )]

                if rel.target == to_page:
                    return KnowledgePath(
                        nodes=new_path,
                        total_strength=strength * rel.strength,
                        length=len(new_path),
                    )

                if rel.target not in visited:
                    heappush(queue, (-strength * rel.strength, rel.target, new_path))

        return None

    def get_related_cluster(self, page: str, depth: int = 2,
                            min_strength: float = 0.3) -> Set[str]:
        """获取页面的关联簇（N 度邻居）"""
        cluster = {page}
        current_layer = {page}

        for _ in range(depth):
            next_layer = set()
            for node in current_layer:
                rels = self.get_relations(node, min_confidence=min_strength)
                for rel in rels:
                    if rel.strength >= min_strength:
                        next_layer.add(rel.target)
                # 入边也考虑
                incoming = self.get_incoming_relations(node, min_confidence=min_strength)
                for rel in incoming:
                    if rel.strength >= min_strength:
                        next_layer.add(rel.source)
            cluster.update(next_layer)
            current_layer = next_layer - cluster
            if not current_layer:
                break

        return cluster

    # ========== 冲突检测（知识免疫系统接口）==========

    def detect_conflicts(self) -> List[Tuple[Relation, Relation, str]]:
        """
        检测知识冲突

        返回 [(关系1, 关系2, 冲突描述), ...]

        冲突类型：
        1. 直接矛盾：A contradicts B 且 B contradicts A（正常）
        2. 逻辑矛盾：A builds_on B 且 A contradicts B
        3. 替代矛盾：A replaces B 且 B replaces A（循环替代）
        4. 演化矛盾：A evolved_from B 且 B evolved_from A
        """
        conflicts = []

        with self._conn() as conn:
            # 获取所有关系
            rows = conn.execute(
                "SELECT source, target, relation_type FROM relations"
            ).fetchall()

        rel_set = set()
        for row in rows:
            rel_set.add((row["source"], row["target"], row["relation_type"]))

        for source, target, rel_type in rel_set:
            # 检查逻辑矛盾
            if rel_type == RelationType.BUILDS_ON.value:
                # A builds_on B 但 A contradicts B
                if (source, target, RelationType.CONTRADICTS.value) in rel_set:
                    conflicts.append((
                        Relation(source=source, target=target, relation_type=RelationType.BUILDS_ON),
                        Relation(source=source, target=target, relation_type=RelationType.CONTRADICTS),
                        f"'{source}' 既建立在 '{target}' 之上，又与它矛盾",
                    ))

            if rel_type == RelationType.REPLACES.value:
                # A replaces B 且 B replaces A
                if (target, source, RelationType.REPLACES.value) in rel_set:
                    conflicts.append((
                        Relation(source=source, target=target, relation_type=RelationType.REPLACES),
                        Relation(source=target, target=source, relation_type=RelationType.REPLACES),
                        f"'{source}' 和 '{target}' 互相替代，形成循环",
                    ))

            if rel_type == RelationType.EVOLVED_FROM.value:
                # A evolved_from B 且 B evolved_from A
                if (target, source, RelationType.EVOLVED_FROM.value) in rel_set:
                    conflicts.append((
                        Relation(source=source, target=target, relation_type=RelationType.EVOLVED_FROM),
                        Relation(source=target, target=source, relation_type=RelationType.EVOLVED_FROM),
                        f"'{source}' 和 '{target}' 互相演化，形成循环",
                    ))

        return conflicts

    def get_contradiction_pairs(self, page: str) -> List[Relation]:
        """获取与某页面存在矛盾关系的所有页面"""
        contradictions = []

        # 出边 contradicts
        out_rels = self.get_relations(page, relation_type=RelationType.CONTRADICTS)
        contradictions.extend(out_rels)

        # 入边 contradicts（对称关系自动维护，但保险起见也查）
        in_rels = self.get_incoming_relations(page)
        for rel in in_rels:
            if rel.relation_type == RelationType.CONTRADICTS:
                contradictions.append(rel)

        return contradictions

    # ========== 导出 ==========

    def export_mermaid(self, page: str, depth: int = 1,
                       min_strength: float = 0.3) -> str:
        """
        导出 Mermaid 图（用于嵌入 Obsidian）

        Returns:
            Mermaid flowchart 语法字符串
        """
        cluster = self.get_related_cluster(page, depth=depth, min_strength=min_strength)

        lines = ["```mermaid", "flowchart TD"]
        node_ids = {}

        for i, node in enumerate(cluster):
            node_id = f"N{i}"
            node_ids[node] = node_id
            # 简化显示：只显示文件名
            label = Path(node).stem if "/" in node else node
            lines.append(f"    {node_id}[\"{label}\"]")

        # 收集关系边
        edges = set()
        for node in cluster:
            rels = self.get_relations(node, min_confidence=min_strength)
            for rel in rels:
                if rel.target in cluster and rel.strength >= min_strength:
                    edge_key = (node_ids.get(node), node_ids.get(rel.target), rel.relation_type.value)
                    if edge_key not in edges:
                        edges.add(edge_key)
                        lines.append(
                            f"    {edge_key[0]} -->|{rel.relation_type.value}({rel.strength:.1f})| {edge_key[1]}"
                        )

        lines.append("```")
        return "\n".join(lines)

    def export_dataview_query(self, page: str) -> str:
        """
        导出 Dataview 查询（Obsidian 插件）

        生成可在 Obsidian Dataview 中运行的查询语句
        """
        return f"""```dataview
TABLE relation_type, strength, confidence
FROM ""
WHERE file.path = "{page}"
```

> Dataview 目前不支持直接查询外部关系数据库。
> 建议将关键关系同步到 frontmatter 的 `relations` 字段后使用。
"""

    def export_frontmatter_relations(self, page: str) -> List[Dict]:
        """
        导出适合写入 frontmatter 的关系列表

        Returns:
            [{target, type, strength}, ...]
        """
        rels = self.get_relations(page)
        return [
            {
                "target": Path(rel.target).stem if "/" in rel.target else rel.target,
                "type": rel.relation_type.value,
                "strength": round(rel.strength, 2),
            }
            for rel in rels
        ]

    # ========== 统计 ==========

    def get_stats(self) -> Dict:
        """获取图谱统计"""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
            type_counts = conn.execute(
                "SELECT relation_type, COUNT(*) FROM relations GROUP BY relation_type"
            ).fetchall()
            avg_confidence = conn.execute(
                "SELECT AVG(confidence) FROM relations"
            ).fetchone()[0] or 0
            avg_strength = conn.execute(
                "SELECT AVG(strength) FROM relations"
            ).fetchone()[0] or 0

        return {
            "total_relations": total,
            "type_distribution": {row[0]: row[1] for row in type_counts},
            "avg_confidence": round(avg_confidence, 3),
            "avg_strength": round(avg_strength, 3),
        }

    def get_hub_pages(self, top_n: int = 10) -> List[Tuple[str, int]]:
        """获取枢纽页面（连接数最多的页面）"""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT source, COUNT(*) as out_count
                FROM relations
                GROUP BY source
                ORDER BY out_count DESC
                LIMIT ?
            """, (top_n,)).fetchall()
        return [(row[0], row[1]) for row in rows]

    # ========== 辅助方法 ==========

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        """提取 frontmatter"""
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    import yaml
                    return yaml.safe_load(parts[1]) or {}
                except Exception:
                    pass
        return {}

    @staticmethod
    def _extract_title(content: str) -> str:
        """提取标题"""
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _extract_wiki_links(content: str) -> List[str]:
        """提取 [[链接]]"""
        links = re.findall(r"\[\[([^\]]+)\]\]", content)
        # 处理别名：[[目标|显示名]]
        return [link.split("|")[0].strip() for link in links]

    @staticmethod
    def _extract_all_keywords(frontmatter: Dict) -> List[str]:
        """提取所有关键词"""
        keywords = []

        # 分层关键词
        kw_dict = frontmatter.get("关键词", {})
        if isinstance(kw_dict, dict):
            for layer_words in kw_dict.values():
                if isinstance(layer_words, list):
                    keywords.extend(layer_words)

        # 其他可能的关键词字段
        for field in ["领域", "类型", "版本标记"]:
            val = frontmatter.get(field)
            if val and isinstance(val, str):
                keywords.append(val)

        # 触发场景
        scenes = frontmatter.get("触发场景", [])
        if isinstance(scenes, list):
            keywords.extend(scenes)

        return [k.lower() for k in keywords if isinstance(k, str)]


# ========== 便捷函数 ==========

def build_graph_for_wiki(wiki_base: str = None) -> KnowledgeGraph:
    """为整个 Wiki 构建知识图谱（全量扫描）"""
    kg = KnowledgeGraph(wiki_base=wiki_base)
    wiki_path = kg.wiki_base / "00-Inbox"

    if not wiki_path.exists():
        return kg

    all_pages = list(wiki_path.glob("*.md"))

    for page in all_pages:
        relations = kg.discover_relations(page, all_pages)
        kg.apply_discovered(relations)

    return kg
