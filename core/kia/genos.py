"""
Knowledge DNA - 知识 DNA 指纹系统

为每个 Wiki 页面生成多维度指纹，用于：
1. 去重检测（避免相似内容重复入库）
2. 自动聚类（发现知识主题簇）
3. 隐含关系推断（相似 DNA 的知识自动建立关系）
4. 版本追踪（识别同一知识的演化版本）

设计原则：
- 零外部依赖（不引入 embedding 模型）
- 基于已有 frontmatter 和文本特征
- 多维度指纹组合，降低误判率
- 轻量高效，适合本地运行
"""
# Genos — 起源/DNA — 知识 DNA 编码，知识的遗传结构
# 原模块: knowledge_dna.py



import json
import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from core.config import get_config
import logging

logger = logging.getLogger(__name__)


# ========== DNA 数据模型 ==========

@dataclass
class KnowledgeDNA:
    """知识 DNA 指纹"""
    page_path: str

    # 维度1：内容指纹（精确去重）
    content_md5: str = ""           # 全文 MD5
    content_simhash: str = ""       # SimHash（局部敏感哈希）

    # 维度2：结构指纹（主题相似）
    semantic_signature: str = ""    # 领域+类型+复杂度+情感的组合签名
    domain_type_hash: str = ""      # 领域和类型的哈希

    # 维度3：关键词指纹（标签相似）
    keyword_set: Set[str] = field(default_factory=set)      # 所有关键词集合
    core_concepts: Set[str] = field(default_factory=set)    # 核心概念
    tool_entities: Set[str] = field(default_factory=set)    # 工具实体
    scenario_tags: Set[str] = field(default_factory=set)    # 场景标签

    # 维度4：标题指纹（语义方向）
    title_keywords: Set[str] = field(default_factory=set)   # 标题关键词
    title_pattern: str = ""         # 标题模式（问题/结论/指南）

    # 维度5：质量指纹（可信度评估）
    confidence: float = 0.0         # 原始置信度
    evidence_level: str = ""        # 证据级别
    temporal: str = ""              # 时效性

    # 元信息
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict:
        """序列化"""
        return {
            "page_path": self.page_path,
            "content_md5": self.content_md5,
            "content_simhash": self.content_simhash,
            "semantic_signature": self.semantic_signature,
            "domain_type_hash": self.domain_type_hash,
            "keyword_set": sorted(self.keyword_set),
            "core_concepts": sorted(self.core_concepts),
            "tool_entities": sorted(self.tool_entities),
            "scenario_tags": sorted(self.scenario_tags),
            "title_keywords": sorted(self.title_keywords),
            "title_pattern": self.title_pattern,
            "confidence": self.confidence,
            "evidence_level": self.evidence_level,
            "temporal": self.temporal,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "KnowledgeDNA":
        """反序列化"""
        return cls(
            page_path=data.get("page_path", ""),
            content_md5=data.get("content_md5", ""),
            content_simhash=data.get("content_simhash", ""),
            semantic_signature=data.get("semantic_signature", ""),
            domain_type_hash=data.get("domain_type_hash", ""),
            keyword_set=set(data.get("keyword_set", [])),
            core_concepts=set(data.get("core_concepts", [])),
            tool_entities=set(data.get("tool_entities", [])),
            scenario_tags=set(data.get("scenario_tags", [])),
            title_keywords=set(data.get("title_keywords", [])),
            title_pattern=data.get("title_pattern", ""),
            confidence=data.get("confidence", 0.0),
            evidence_level=data.get("evidence_level", ""),
            temporal=data.get("temporal", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class SimilarityResult:
    """相似度分析结果"""
    target_page: str
    overall_score: float           # 综合相似度 0-1
    dimension_scores: Dict[str, float]  # 各维度得分
    verdict: str                   # duplicate / related / distinct
    reason: str                    # 判断理由


# ========== SimHash 实现（轻量版）==========

class SimHash:
    """简化版 SimHash，用于文本近似去重"""

    HASH_BITS = 64

    @classmethod
    def compute(cls, text: str) -> str:
        """计算文本的 SimHash"""
        # 1. 清洗文本
        text = cls._normalize(text)

        # 2. 生成 2-gram
        tokens = cls._tokenize(text)
        if not tokens:
            return "0" * cls.HASH_BITS

        # 3. 加权累加
        vector = [0] * cls.HASH_BITS
        for token in tokens:
            weight = cls._token_weight(token)
            hash_val = cls._hash_token(token)
            for i in range(cls.HASH_BITS):
                if hash_val & (1 << i):
                    vector[i] += weight
                else:
                    vector[i] -= weight

        # 4. 取符号
        fingerprint = 0
        for i in range(cls.HASH_BITS):
            if vector[i] > 0:
                fingerprint |= (1 << i)

        return format(fingerprint, f"0{cls.HASH_BITS // 4}x")

    @classmethod
    def hamming_distance(cls, hash1: str, hash2: str) -> int:
        """计算两个 SimHash 的汉明距离"""
        if not hash1 or not hash2:
            return cls.HASH_BITS
        try:
            x = int(hash1, 16)
            y = int(hash2, 16)
            xor = x ^ y
            return bin(xor).count("1")
        except ValueError:
            return cls.HASH_BITS

    @classmethod
    def similarity(cls, hash1: str, hash2: str) -> float:
        """基于汉明距离的相似度"""
        dist = cls.hamming_distance(hash1, hash2)
        return max(0.0, 1.0 - dist / (cls.HASH_BITS / 2))

    @staticmethod
    def _normalize(text: str) -> str:
        """文本归一化"""
        # 去标点、去空白、转小写
        text = re.sub(r"[^\w一-鿿]", "", text)
        return text.lower()

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """分词：中文按字，英文按词"""
        tokens = []
        # 2-gram
        for i in range(len(text) - 1):
            tokens.append(text[i:i + 2])
        return tokens

    @staticmethod
    def _token_weight(token: str) -> int:
        """词权重：长词权重更高"""
        return min(len(token), 5)

    @staticmethod
    def _hash_token(token: str) -> int:
        """词的哈希值"""
        return int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)


# ========== DNA 计算引擎 ==========

class DNAEngine:
    """知识 DNA 计算引擎"""

    # 相似度阈值
    DUPLICATE_THRESHOLD = 0.90     # 疑似重复
    RELATED_THRESHOLD = 0.65       # 相关但不同
    CLUSTER_THRESHOLD = 0.50       # 同一知识簇

    # 维度权重
    WEIGHTS = {
        "content": 0.30,           # 内容相似度
        "semantic": 0.25,          # 语义签名匹配
        "keyword": 0.25,           # 关键词重叠
        "title": 0.15,             # 标题相似度
        "structure": 0.05,         # 结构匹配（置信度/证据级别）
    }

    def __init__(self, db_path: str = None, wiki_base: str = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.db_path = Path(db_path) if db_path else (
            self.wiki_base / ".kg" / "dna.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        schema = """
        CREATE TABLE IF NOT EXISTS knowledge_dna (
            page_path TEXT PRIMARY KEY,
            content_md5 TEXT,
            content_simhash TEXT,
            semantic_signature TEXT,
            domain_type_hash TEXT,
            keyword_set TEXT,        -- JSON array
            core_concepts TEXT,      -- JSON array
            tool_entities TEXT,      -- JSON array
            scenario_tags TEXT,      -- JSON array
            title_keywords TEXT,     -- JSON array
            title_pattern TEXT,
            confidence REAL,
            evidence_level TEXT,
            temporal TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dna_md5 ON knowledge_dna(content_md5);
        CREATE INDEX IF NOT EXISTS idx_dna_simhash ON knowledge_dna(content_simhash);
        CREATE INDEX IF NOT EXISTS idx_dna_signature ON knowledge_dna(semantic_signature);
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(schema)

    def compute_dna(self, page_path: Path) -> Optional[KnowledgeDNA]:
        """
        计算页面的 DNA 指纹

        Args:
            page_path: Wiki 页面文件路径

        Returns:
            KnowledgeDNA 或 None
        """
        if not page_path.exists():
            return None

        try:
            content = page_path.read_text(encoding="utf-8")
        except Exception:
            return None

        frontmatter = self._extract_frontmatter(content)
        body = self._extract_body(content)
        title = self._extract_title(content) or page_path.stem

        dna = KnowledgeDNA(page_path=str(page_path))

        # 维度1：内容指纹
        dna.content_md5 = hashlib.md5(content.encode("utf-8")).hexdigest()
        dna.content_simhash = SimHash.compute(body)

        # 维度2：结构指纹
        domain = frontmatter.get("领域", "其他")
        knowledge_type = frontmatter.get("类型", "未知")
        complexity = frontmatter.get("复杂度", "入门")
        emotion = frontmatter.get("情感倾向", "中性")
        dna.semantic_signature = f"{domain}:{knowledge_type}:{complexity}:{emotion}"
        dna.domain_type_hash = hashlib.md5(
            f"{domain}:{knowledge_type}".encode("utf-8")
        ).hexdigest()[:16]

        # 维度3：关键词指纹
        keywords = frontmatter.get("关键词", {})
        if isinstance(keywords, dict):
            dna.core_concepts = set(keywords.get("核心概念", []))
            dna.scenario_tags = set(keywords.get("场景标签", []))
            dna.tool_entities = set(keywords.get("工具实体", []))
            action_tags = set(keywords.get("动作标签", []))
        else:
            dna.core_concepts, dna.scenario_tags, dna.tool_entities, action_tags = set(), set(), set(), set()

        dna.keyword_set = dna.core_concepts | dna.scenario_tags | dna.tool_entities | action_tags

        # 维度4：标题指纹
        dna.title_keywords = set(self._extract_title_keywords(title))
        dna.title_pattern = self._classify_title_pattern(title)

        # 维度5：质量指纹
        dna.confidence = float(frontmatter.get("置信度", 0.5))
        dna.evidence_level = frontmatter.get("证据级别", "单源")
        dna.temporal = frontmatter.get("时效性", "上下文相关")

        dna.created_at = datetime.now().isoformat()[:19]
        dna.updated_at = dna.created_at

        return dna

    def save_dna(self, dna: KnowledgeDNA) -> bool:
        """保存 DNA 到数据库"""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO knowledge_dna
                       (page_path, content_md5, content_simhash, semantic_signature,
                        domain_type_hash, keyword_set, core_concepts, tool_entities,
                        scenario_tags, title_keywords, title_pattern, confidence,
                        evidence_level, temporal, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        dna.page_path,
                        dna.content_md5,
                        dna.content_simhash,
                        dna.semantic_signature,
                        dna.domain_type_hash,
                        json.dumps(sorted(dna.keyword_set), ensure_ascii=False),
                        json.dumps(sorted(dna.core_concepts), ensure_ascii=False),
                        json.dumps(sorted(dna.tool_entities), ensure_ascii=False),
                        json.dumps(sorted(dna.scenario_tags), ensure_ascii=False),
                        json.dumps(sorted(dna.title_keywords), ensure_ascii=False),
                        dna.title_pattern,
                        dna.confidence,
                        dna.evidence_level,
                        dna.temporal,
                        dna.created_at,
                        dna.updated_at,
                    )
                )
                conn.commit()
                return True
        except sqlite3.Error:
            return False

    def load_dna(self, page_path: str) -> Optional[KnowledgeDNA]:
        """从数据库加载 DNA"""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM knowledge_dna WHERE page_path = ?",
                    (page_path,)
                ).fetchone()

            if not row:
                return None

            return KnowledgeDNA(
                page_path=row["page_path"],
                content_md5=row["content_md5"],
                content_simhash=row["content_simhash"],
                semantic_signature=row["semantic_signature"],
                domain_type_hash=row["domain_type_hash"],
                keyword_set=set(json.loads(row["keyword_set"] or "[]")),
                core_concepts=set(json.loads(row["core_concepts"] or "[]")),
                tool_entities=set(json.loads(row["tool_entities"] or "[]")),
                scenario_tags=set(json.loads(row["scenario_tags"] or "[]")),
                title_keywords=set(json.loads(row["title_keywords"] or "[]")),
                title_pattern=row["title_pattern"],
                confidence=row["confidence"],
                evidence_level=row["evidence_level"],
                temporal=row["temporal"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception:
            return None

    def compare(self, dna1: KnowledgeDNA, dna2: KnowledgeDNA) -> SimilarityResult:
        """
        比较两个 DNA 的相似度

        返回综合相似度和各维度得分
        """
        # 维度1：内容相似度（SimHash）
        content_sim = SimHash.similarity(dna1.content_simhash, dna2.content_simhash)

        # 维度2：语义签名匹配
        semantic_sim = 1.0 if dna1.semantic_signature == dna2.semantic_signature else 0.0
        # 如果领域和类型相同，给部分分
        if not semantic_sim and dna1.domain_type_hash == dna2.domain_type_hash:
            semantic_sim = 0.5

        # 维度3：关键词 Jaccard 相似度
        keyword_sim = self._jaccard(dna1.keyword_set, dna2.keyword_set)
        # 核心概念权重更高
        core_sim = self._jaccard(dna1.core_concepts, dna2.core_concepts)
        tool_sim = self._jaccard(dna1.tool_entities, dna2.tool_entities)
        keyword_sim = keyword_sim * 0.4 + core_sim * 0.4 + tool_sim * 0.2

        # 维度4：标题相似度
        title_sim = self._jaccard(dna1.title_keywords, dna2.title_keywords)
        # 标题模式相同加分
        if dna1.title_pattern and dna1.title_pattern == dna2.title_pattern:
            title_sim = min(title_sim + 0.2, 1.0)

        # 维度5：结构匹配
        structure_sim = 0.0
        if dna1.evidence_level == dna2.evidence_level:
            structure_sim += 0.5
        if dna1.temporal == dna2.temporal:
            structure_sim += 0.5

        # 综合加权
        overall = (
            content_sim * self.WEIGHTS["content"] +
            semantic_sim * self.WEIGHTS["semantic"] +
            keyword_sim * self.WEIGHTS["keyword"] +
            title_sim * self.WEIGHTS["title"] +
            structure_sim * self.WEIGHTS["structure"]
        )

        # 判断结论
        if overall >= self.DUPLICATE_THRESHOLD:
            verdict = "duplicate"
            reason = "内容高度相似，疑似重复"
        elif overall >= self.RELATED_THRESHOLD:
            verdict = "related"
            reason = "主题相关但内容不同"
        elif overall >= self.CLUSTER_THRESHOLD:
            verdict = "cluster"
            reason = "同一知识簇，关联较弱"
        else:
            verdict = "distinct"
            reason = "内容独立，无显著关联"

        return SimilarityResult(
            target_page=dna2.page_path,
            overall_score=round(overall, 3),
            dimension_scores={
                "content": round(content_sim, 3),
                "semantic": round(semantic_sim, 3),
                "keyword": round(keyword_sim, 3),
                "title": round(title_sim, 3),
                "structure": round(structure_sim, 3),
            },
            verdict=verdict,
            reason=reason,
        )

    def find_similar(self, dna: KnowledgeDNA,
                     threshold: float = None,
                     exclude_self: bool = True) -> List[SimilarityResult]:
        """
        查找与给定 DNA 相似的页面

        Args:
            dna: 目标 DNA
            threshold: 相似度阈值（默认 RELATED_THRESHOLD）
            exclude_self: 排除自身

        Returns:
            相似度结果列表（按得分降序）
        """
        threshold = threshold or self.RELATED_THRESHOLD

        results = []
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM knowledge_dna").fetchall()

        for row in rows:
            other_path = row["page_path"]
            if exclude_self and other_path == dna.page_path:
                continue

            other = self._row_to_dna(row)
            result = self.compare(dna, other)
            if result.overall_score >= threshold:
                results.append(result)

        results.sort(key=lambda x: x.overall_score, reverse=True)
        return results

    def find_duplicates(self, dna: KnowledgeDNA) -> List[SimilarityResult]:
        """查找疑似重复的页面"""
        return self.find_similar(dna, threshold=self.DUPLICATE_THRESHOLD)

    def find_cluster(self, dna: KnowledgeDNA,
                     depth: int = 2) -> Set[str]:
        """
        查找 DNA 所属的知识簇（连通分量）

        使用 BFS，从目标 DNA 出发，逐步扩展到相似度达标的邻居
        """
        cluster = {dna.page_path}
        current_layer = {dna.page_path}

        for _ in range(depth):
            next_layer = set()
            for page_path in current_layer:
                page_dna = self.load_dna(page_path)
                if not page_dna:
                    continue
                similar = self.find_similar(page_dna, threshold=self.CLUSTER_THRESHOLD)
                for result in similar:
                    next_layer.add(result.target_page)
            cluster.update(next_layer)
            current_layer = next_layer - cluster
            if not current_layer:
                break

        return cluster

    def scan_all_pages(self) -> Dict[str, int]:
        """
        全量扫描 Wiki 目录，为所有页面计算 DNA

        Returns:
            {状态: 数量} 统计
        """
        inbox = self.wiki_base / "00-Inbox"
        if not inbox.exists():
            return {"scanned": 0, "computed": 0, "failed": 0}

        stats = {"scanned": 0, "computed": 0, "failed": 0}

        for page in inbox.glob("*.md"):
            stats["scanned"] += 1
            dna = self.compute_dna(page)
            if dna:
                if self.save_dna(dna):
                    stats["computed"] += 1
                else:
                    stats["failed"] += 1
            else:
                stats["failed"] += 1

        return stats

    def get_stats(self) -> Dict:
        """获取 DNA 库统计"""
        with sqlite3.connect(str(self.db_path)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM knowledge_dna").fetchone()[0]

            # 签名分布
            sig_rows = conn.execute(
                "SELECT semantic_signature, COUNT(*) FROM knowledge_dna GROUP BY semantic_signature"
            ).fetchall()

            # 模式分布
            pattern_rows = conn.execute(
                "SELECT title_pattern, COUNT(*) FROM knowledge_dna GROUP BY title_pattern"
            ).fetchall()

        return {
            "total_fingerprints": total,
            "signature_distribution": {r[0]: r[1] for r in sig_rows},
            "pattern_distribution": {r[0] or "unknown": r[1] for r in pattern_rows},
        }

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
                except Exception as e:
                    logger.warning(f"忽略异常: {e}")
        return {}

    @staticmethod
    def _extract_body(content: str) -> str:
        """提取正文（去掉 frontmatter）"""
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2]
        return content

    @staticmethod
    def _extract_title(content: str) -> str:
        """提取标题"""
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _extract_title_keywords(title: str) -> List[str]:
        """提取标题关键词"""
        # 去掉常见虚词
        stopwords = {"的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
                     "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
                     "你", "会", "着", "没有", "看", "好", "自己", "这", "那",
                     "如何", "为什么", "怎么", "什么", "哪些", "怎么", "怎样",
                     "the", "a", "an", "is", "are", "was", "were", "be", "been",
                     "to", "of", "and", "in", "on", "at", "for", "with", "by",
                     "how", "what", "why", "when", "where", "which"}

        # 保留中英文词、技术术语
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_.]*|[一-鿿]{2,}", title)
        return [w for w in words if w.lower() not in stopwords and len(w) > 1]

    @staticmethod
    def _classify_title_pattern(title: str) -> str:
        """分类标题模式"""
        title_lower = title.lower()

        if any(w in title_lower for w in ["为什么", "为何", "how come", "why", "怎么回事"]):
            return "question_why"
        if any(w in title_lower for w in ["怎么", "如何", "how to", "怎样", "怎么做"]):
            return "question_how"
        if any(w in title_lower for w in ["什么", "which", "what is", "什么是"]):
            return "question_what"
        if any(w in title_lower for w in ["选", "还是", "vs", "versus", "对比", "区别", "difference"]):
            return "comparison"
        if any(w in title_lower for w in ["避免", "不要", "切忌", "anti", "避免"]):
            return "anti_pattern"
        if any(w in title_lower for w in ["步骤", "流程", "指南", "guide", "步骤", " tutorial"]):
            return "methodology"
        if any(w in title_lower for w in ["原则", "法则", "经验", "rule", "principle", "heuristic"]):
            return "heuristic"
        if any(w in title_lower for w in ["解决", "修复", "排查", "fix", "solve", "debug"]):
            return "problem_solution"

        return "statement"

    @staticmethod
    def _jaccard(set1: Set, set2: Set) -> float:
        """计算 Jaccard 相似度"""
        if not set1 and not set2:
            return 1.0
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        return intersection / union if union > 0 else 0.0

    def _row_to_dna(self, row: sqlite3.Row) -> KnowledgeDNA:
        """数据库行转 DNA"""
        return KnowledgeDNA(
            page_path=row["page_path"],
            content_md5=row["content_md5"],
            content_simhash=row["content_simhash"],
            semantic_signature=row["semantic_signature"],
            domain_type_hash=row["domain_type_hash"],
            keyword_set=set(json.loads(row["keyword_set"] or "[]")),
            core_concepts=set(json.loads(row["core_concepts"] or "[]")),
            tool_entities=set(json.loads(row["tool_entities"] or "[]")),
            scenario_tags=set(json.loads(row["scenario_tags"] or "[]")),
            title_keywords=set(json.loads(row["title_keywords"] or "[]")),
            title_pattern=row["title_pattern"],
            confidence=row["confidence"],
            evidence_level=row["evidence_level"],
            temporal=row["temporal"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ========== 便捷函数 ==========

def compute_and_save(page_path: str, engine: DNAEngine = None) -> Optional[KnowledgeDNA]:
    """便捷函数：计算并保存 DNA"""
    engine = engine or DNAEngine()
    dna = engine.compute_dna(Path(page_path))
    if dna:
        engine.save_dna(dna)
    return dna


def check_duplicate(page_path: str, engine: DNAEngine = None) -> List[SimilarityResult]:
    """便捷函数：检查页面是否重复"""
    engine = engine or DNAEngine()
    dna = engine.compute_dna(Path(page_path))
    if not dna:
        return []
    engine.save_dna(dna)
    return engine.find_duplicates(dna)
