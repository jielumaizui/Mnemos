"""
Knowledge Quantum Entanglement - 知识量子纠缠

发现知识结构中表面无关但深层关联的知识对：
1. 间接路径关联 — 通过知识图谱路径 A→B→C 发现 A 和 C 的隐性关联
2. 语义深层关联 — 表面不相似但语义向量相近（利用 DNA 语义签名）
3. 跨域共振 — 不同领域但解决相同底层问题的知识
4. 互补纠缠 — 两个知识单独看不完整，组合后形成完整体系

与 Dark Knowledge Mining 的区别：
- Dark Knowledge: 从用户行为中发现（观察到的关联）
- Quantum Entanglement: 从知识结构本身发现（内在的关联）

设计原则：
- 利用已有的知识图谱和 DNA 数据，不重复计算
- 纠缠发现是计算密集型操作，支持采样和缓存
- 输出"纠缠对"建议，由用户确认后建立关系
"""
# Moirai — 命运三女神 — 量子纠缠，知识节点的命运交织
# 原模块: quantum_entanglement.py



import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime
from collections import Counter, defaultdict, deque
from core.config import get_config
import logging

logger = logging.getLogger(__name__)


@dataclass
class EntanglementPair:
    """纠缠对"""
    page_a: str
    page_b: str
    page_a_title: str = ""
    page_b_title: str = ""
    entanglement_type: str = ""       # indirect_path / semantic_deep / cross_domain / complementary
    connection_strength: float = 0.0   # 纠缠强度 0-1
    path_description: str = ""         # 如何关联的描述
    shared_concepts: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)


@dataclass
class EntanglementNetwork:
    """纠缠网络"""
    pairs: List[EntanglementPair] = field(default_factory=list)
    hub_pages: List[Dict] = field(default_factory=list)  # 高度纠缠的枢纽页面
    cluster_map: Dict[str, List[str]] = field(default_factory=dict)  # 聚类结果


class QuantumEntanglement:
    """知识量子纠缠发现器"""

    # 纠缠强度阈值
    STRONG_ENTANGLEMENT = 0.80
    MEDIUM_ENTANGLEMENT = 0.60
    WEAK_ENTANGLEMENT = 0.40

    def __init__(self, wiki_base: str = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.inbox = self.wiki_base / "00-Inbox"
        self.kg_db = self.wiki_base / ".kg" / "knowledge_graph.db"
        self.dna_dir = self.wiki_base / ".kg" / "dna"

    def _kg_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.kg_db), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ========== 间接路径关联 ==========

    def discover_indirect_paths(self, max_depth: int = 3,
                                 min_strength: float = 0.3) -> List[EntanglementPair]:
        """
        通过知识图谱发现间接关联

        算法：BFS 寻找最短路径，路径越长强度越低
        """
        if not self.kg_db.exists():
            return []

        # 加载所有关系
        with self._kg_conn() as conn:
            rows = conn.execute(
                "SELECT source, target, relation_type, strength FROM relations"
            ).fetchall()

        # 构建邻接表
        graph = defaultdict(list)
        for row in rows:
            graph[row["source"]].append((row["target"], row["strength"]))
            # 无向化（某些关系是对称的）
            if row["relation_type"] in ("similar_to", "related_to", "contradicts"):
                graph[row["target"]].append((row["source"], row["strength"]))

        # 获取所有页面
        all_pages = set(graph.keys())
        for targets in graph.values():
            all_pages.update(t[0] for t in targets)

        # 寻找间接路径
        pairs = []
        checked = set()

        for start_page in list(all_pages)[:50]:  # 采样限制
            paths = self._bfs_paths(graph, start_page, max_depth)

            for end_page, path, total_strength in paths:
                if start_page == end_page:
                    continue

                pair_key = tuple(sorted([start_page, end_page]))
                if pair_key in checked:
                    continue
                checked.add(pair_key)

                # 过滤已有直接关系的对
                if self._has_direct_relation(start_page, end_page):
                    continue

                if total_strength >= min_strength:
                    pairs.append(EntanglementPair(
                        page_a=start_page,
                        page_b=end_page,
                        entanglement_type="indirect_path",
                        connection_strength=round(total_strength, 3),
                        path_description=" → ".join(
                            [Path(p).stem for p in path]
                        ),
                        evidence=[f"路径: {' → '.join(path)}"],
                    ))

        pairs.sort(key=lambda x: x.connection_strength, reverse=True)
        return pairs

    def _bfs_paths(self, graph: Dict, start: str, max_depth: int) -> List[Tuple[str, List[str], float]]:
        """BFS 寻找路径，返回 (终点, 路径, 累计强度)"""
        results = []
        visited = {start}
        queue = deque([(start, [start], 1.0)])

        while queue:
            current, path, strength = queue.popleft()

            if len(path) > max_depth:
                continue

            for neighbor, edge_strength in graph.get(current, []):
                if neighbor in visited:
                    continue

                new_strength = strength * edge_strength
                new_path = path + [neighbor]

                # 找到新终点
                if len(new_path) > 2:  # 至少经过中间节点
                    results.append((neighbor, new_path, new_strength))

                if len(new_path) < max_depth:
                    visited.add(neighbor)
                    queue.append((neighbor, new_path, new_strength))

        return results

    def _has_direct_relation(self, a: str, b: str) -> bool:
        """检查两个页面是否已有直接关系"""
        if not self.kg_db.exists():
            return False
        with self._kg_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM relations WHERE (source=? AND target=?) OR (source=? AND target=?)",
                (a, b, b, a)
            ).fetchone()
        return row is not None

    # ========== 语义深层关联 ==========

    def discover_semantic_deep(self, sample_size: int = 100,
                                min_similarity: float = 0.55) -> List[EntanglementPair]:
        """
        发现语义深层关联

        利用 DNA 引擎的语义签名，找到表面关键词不重叠但语义相近的知识
        """
        try:
            from .genos import DNAEngine
            dna_engine = DNAEngine(wiki_base=str(self.wiki_base))
        except ImportError:
            return []

        # 加载所有 DNA
        dnas = []
        if self.dna_dir.exists():
            for f in self.dna_dir.glob("*.json"):
                dna = dna_engine.load_dna(f)
                if dna:
                    dnas.append(dna)

        if len(dnas) < 2:
            return []

        # 限制样本
        if sample_size and len(dnas) > sample_size:
            import random
            random.seed(42)
            dnas = random.sample(dnas, sample_size)

        pairs = []
        checked = set()

        for i, dna_a in enumerate(dnas):
            for j, dna_b in enumerate(dnas[i + 1:], i + 1):
                pair_key = tuple(sorted([dna_a.page_path, dna_b.page_path]))
                if pair_key in checked:
                    continue
                checked.add(pair_key)

                # 跳过已有直接关系的
                if self._has_direct_relation(dna_a.page_path, dna_b.page_path):
                    continue

                # 计算语义相似度（侧重 semantic_signature 维度）
                result = dna_engine.compare(dna_a, dna_b)

                # 重点看语义维度，降低关键词维度的权重
                semantic_score = self._extract_semantic_score(result)

                # 关键词重叠度低但语义相似度高 = 深层关联
                keyword_overlap = self._keyword_overlap(dna_a, dna_b)

                if semantic_score >= min_similarity and keyword_overlap < 0.3:
                    # 提取共享概念
                    shared = self._extract_shared_concepts(dna_a, dna_b)

                    pairs.append(EntanglementPair(
                        page_a=dna_a.page_path,
                        page_b=dna_b.page_path,
                        page_a_title=dna_a.title,
                        page_b_title=dna_b.title,
                        entanglement_type="semantic_deep",
                        connection_strength=round(semantic_score, 3),
                        shared_concepts=shared,
                        evidence=[
                            f"语义相似度: {semantic_score:.2f}",
                            f"关键词重叠: {keyword_overlap:.2f}",
                        ],
                    ))

        pairs.sort(key=lambda x: x.connection_strength, reverse=True)
        return pairs

    def _extract_semantic_score(self, compare_result) -> float:
        """从比较结果中提取语义相似度"""
        # 优先使用 semantic_signature 维度的分数
        # compare_result 可能有 dimension_scores 或 overall_score
        if hasattr(compare_result, 'dimension_scores') and compare_result.dimension_scores:
            return compare_result.dimension_scores.get('semantic', compare_result.overall_score)
        if hasattr(compare_result, 'overall_score'):
            return compare_result.overall_score
        return 0.0

    def _keyword_overlap(self, dna_a, dna_b) -> float:
        """计算关键词重叠度"""
        a_keywords = set(dna_a.keyword_set) if hasattr(dna_a, 'keyword_set') else set()
        b_keywords = set(dna_b.keyword_set) if hasattr(dna_b, 'keyword_set') else set()

        if not a_keywords or not b_keywords:
            return 0.0

        intersection = a_keywords & b_keywords
        union = a_keywords | b_keywords
        return len(intersection) / len(union)

    def _extract_shared_concepts(self, dna_a, dna_b) -> List[str]:
        """提取共享概念"""
        concepts = []

        # 从语义签名中提取
        sig_a = getattr(dna_a, 'semantic_signature', '')
        sig_b = getattr(dna_b, 'semantic_signature', '')

        if sig_a and sig_b:
            parts_a = set(sig_a.split(':'))
            parts_b = set(sig_b.split(':'))
            shared = parts_a & parts_b
            concepts.extend(list(shared)[:5])

        return concepts

    # ========== 跨域共振 ==========

    def discover_cross_domain(self) -> List[EntanglementPair]:
        """
        发现跨领域共振

        不同领域但解决相同底层问题的知识
        """
        # 加载所有 frontmatter
        pages = []
        if self.inbox.exists():
            for page in self.inbox.glob("*.md"):
                fm = self._extract_frontmatter(page)
                if fm:
                    pages.append({
                        "path": str(page),
                        "title": fm.get("标题", page.stem),
                        "domain": fm.get("领域", "其他"),
                        "form": fm.get("类型", ""),
                        "tools": self._get_keywords(fm, "工具实体"),
                        "concepts": self._get_keywords(fm, "核心概念"),
                    })

        pairs = []
        checked = set()

        for i, a in enumerate(pages):
            for j, b in enumerate(pages[i + 1:], i + 1):
                if a["domain"] == b["domain"]:
                    continue  # 同领域跳过

                pair_key = tuple(sorted([a["path"], b["path"]]))
                if pair_key in checked:
                    continue
                checked.add(pair_key)

                # 检查是否有工具或概念重叠
                tool_overlap = set(a["tools"]) & set(b["tools"])
                concept_overlap = set(a["concepts"]) & set(b["concepts"])

                if tool_overlap or concept_overlap:
                    strength = (len(tool_overlap) * 0.3 + len(concept_overlap) * 0.2)
                    if strength > 0:
                        shared = list(tool_overlap) + list(concept_overlap)
                        pairs.append(EntanglementPair(
                            page_a=a["path"],
                            page_b=b["path"],
                            page_a_title=a["title"],
                            page_b_title=b["title"],
                            entanglement_type="cross_domain",
                            connection_strength=round(min(strength, 1.0), 3),
                            shared_concepts=shared[:5],
                            evidence=[
                                f"领域: {a['domain']} ↔ {b['domain']}",
                                f"共享: {', '.join(shared[:3])}",
                            ],
                        ))

        pairs.sort(key=lambda x: x.connection_strength, reverse=True)
        return pairs

    # ========== 互补纠缠 ==========

    def discover_complementary(self, min_complement: float = 0.3) -> List[EntanglementPair]:
        """
        发现互补纠缠

        两个知识单独看不完整，组合后形成完整体系
        """
        try:
            from .genos import DNAEngine
            dna_engine = DNAEngine(wiki_base=str(self.wiki_base))
        except ImportError:
            return []

        dnas = []
        if self.dna_dir.exists():
            for f in self.dna_dir.glob("*.json"):
                dna = dna_engine.load_dna(f)
                if dna:
                    dnas.append(dna)

        if len(dnas) < 2:
            return []

        pairs = []
        checked = set()

        for i, dna_a in enumerate(dnas):
            for j, dna_b in enumerate(dnas[i + 1:], i + 1):
                pair_key = tuple(sorted([dna_a.page_path, dna_b.page_path]))
                if pair_key in checked:
                    continue
                checked.add(pair_key)

                # 互补性 = 低相似度 + 高关键词互补
                result = dna_engine.compare(dna_a, dna_b)
                similarity = getattr(result, 'overall_score', 0)

                if similarity > 0.7:
                    continue  # 太相似的不可能是互补

                # 计算互补度
                complement_score = self._calculate_complement(dna_a, dna_b)

                if complement_score >= min_complement:
                    pairs.append(EntanglementPair(
                        page_a=dna_a.page_path,
                        page_b=dna_b.page_path,
                        page_a_title=getattr(dna_a, 'title', ''),
                        page_b_title=getattr(dna_b, 'title', ''),
                        entanglement_type="complementary",
                        connection_strength=round(complement_score, 3),
                        evidence=[
                            f"相似度: {similarity:.2f} (低相似)",
                            f"互补度: {complement_score:.2f}",
                        ],
                    ))

        pairs.sort(key=lambda x: x.connection_strength, reverse=True)
        return pairs

    def _calculate_complement(self, dna_a, dna_b) -> float:
        """计算两个 DNA 的互补度"""
        score = 0.0

        # 关键词互补：A 有 B 没有，且 B 有 A 没有
        a_kw = set(getattr(dna_a, 'keyword_set', []))
        b_kw = set(getattr(dna_b, 'keyword_set', []))

        if a_kw and b_kw:
            a_unique = a_kw - b_kw
            b_unique = b_kw - a_kw
            complement = (len(a_unique) + len(b_unique)) / (len(a_kw) + len(b_kw))
            score += complement * 0.5

        # 工具互补
        a_tools = set(getattr(dna_a, 'tool_entities', []))
        b_tools = set(getattr(dna_b, 'tool_entities', []))

        if a_tools and b_tools:
            tool_complement = len(a_tools ^ b_tools) / max(len(a_tools | b_tools), 1)
            score += tool_complement * 0.3

        # 标题模式互补（一个问题一个答案）
        a_pattern = getattr(dna_a, 'title_pattern', '')
        b_pattern = getattr(dna_b, 'title_pattern', '')

        if a_pattern and b_pattern and a_pattern != b_pattern:
            score += 0.2

        return min(score, 1.0)

    # ========== 综合分析 ==========

    def discover_all(self, limit_per_type: int = 20) -> EntanglementNetwork:
        """运行所有纠缠发现算法"""
        indirect = self.discover_indirect_paths()[:limit_per_type]
        semantic = self.discover_semantic_deep()[:limit_per_type]
        cross = self.discover_cross_domain()[:limit_per_type]
        complement = self.discover_complementary()[:limit_per_type]

        all_pairs = indirect + semantic + cross + complement

        # 找出枢纽页面（参与最多纠缠对的页面）
        page_counts = Counter()
        for p in all_pairs:
            page_counts[p.page_a] += 1
            page_counts[p.page_b] += 1

        hub_pages = [
            {"page": page, "title": Path(page).stem, "entanglement_count": count}
            for page, count in page_counts.most_common(10)
        ]

        # 简单聚类：基于连通分量
        clusters = self._build_clusters(all_pairs)

        return EntanglementNetwork(
            pairs=all_pairs,
            hub_pages=hub_pages,
            cluster_map=clusters,
        )

    def _build_clusters(self, pairs: List[EntanglementPair]) -> Dict[str, List[str]]:
        """基于纠缠对构建聚类"""
        # 使用并查集
        parent = {}

        def find(x):
            if x not in parent:
                parent[x] = x
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]

        def union(x, y):
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        for p in pairs:
            if p.connection_strength >= self.MEDIUM_ENTANGLEMENT:
                union(p.page_a, p.page_b)

        clusters = defaultdict(list)
        for page in parent:
            clusters[find(page)].append(page)

        # 只保留大小 > 2 的聚类
        return {
            f"cluster_{i}": members
            for i, members in enumerate(
                [m for m in clusters.values() if len(m) > 2], 1
            )
        }

    # ========== 报告生成 ==========

    def generate_report(self, network: EntanglementNetwork = None) -> str:
        """生成纠缠报告"""
        if network is None:
            network = self.discover_all()

        lines = [
            "# 知识量子纠缠报告",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d')}",
            f"发现纠缠对: **{len(network.pairs)}**",
            f"枢纽页面: {len(network.hub_pages)}",
            f"知识聚类: {len(network.cluster_map)}",
            "",
        ]

        # 按类型分组
        by_type = defaultdict(list)
        for p in network.pairs:
            by_type[p.entanglement_type].append(p)

        type_names = {
            "indirect_path": "间接路径",
            "semantic_deep": "语义深层",
            "cross_domain": "跨域共振",
            "complementary": "互补纠缠",
        }

        for etype, pairs in by_type.items():
            name = type_names.get(etype, etype)
            lines.extend([f"## {name} ({len(pairs)})", ""])

            for p in pairs[:8]:
                title_a = p.page_a_title or Path(p.page_a).stem
                title_b = p.page_b_title or Path(p.page_b).stem
                lines.append(f"- **{title_a}** ↔ **{title_b}** "
                           f"(强度 {p.connection_strength})")
                if p.path_description:
                    lines.append(f"  路径: {p.path_description}")
                if p.shared_concepts:
                    lines.append(f"  共享: {', '.join(p.shared_concepts[:3])}")
                if p.evidence:
                    lines.append(f"  证据: {'; '.join(p.evidence[:2])}")
                lines.append("")

        if network.hub_pages:
            lines.extend(["## 枢纽页面", ""])
            for h in network.hub_pages[:5]:
                lines.append(f"- **{h['title']}** — 参与 {h['entanglement_count']} 个纠缠对")
            lines.append("")

        if network.cluster_map:
            lines.extend(["## 知识聚类", ""])
            for name, members in network.cluster_map.items():
                titles = [Path(m).stem for m in members[:5]]
                lines.append(f"- **{name}** ({len(members)} 个): {', '.join(titles)}")
                if len(members) > 5:
                    lines.append(f"  ... 等 {len(members) - 5} 个")
            lines.append("")

        return "\n".join(lines)

    # ========== 辅助方法 ==========

    @staticmethod
    def _extract_frontmatter(page: Path) -> Optional[Dict]:
        try:
            import yaml
            content = page.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    return yaml.safe_load(parts[1]) or {}
        except Exception as e:
            logger.warning(f"忽略异常: {e}")
        return {}

    @staticmethod
    def _get_keywords(frontmatter: Dict, layer: str) -> List[str]:
        keywords = frontmatter.get("关键词", {})
        if isinstance(keywords, dict):
            return keywords.get(layer, []) or []
        return []


# ========== 便捷函数 ==========

def discover_entanglements() -> str:
    """便捷函数：运行完整的纠缠发现并返回报告"""
    qe = QuantumEntanglement()
    network = qe.discover_all()
    return qe.generate_report(network)


def get_hub_pages() -> List[Dict]:
    """便捷函数：获取枢纽页面列表"""
    qe = QuantumEntanglement()
    network = qe.discover_all()
    return network.hub_pages
