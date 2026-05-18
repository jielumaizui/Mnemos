"""
Dark Knowledge Mining - 暗知识挖掘

从用户行为轨迹中挖掘隐性知识：
1. 隐性关联 — 查询序列中的频繁共现（用户从A到B意味着A→B有关联）
2. 时间模式 — 什么时段/什么情境下查询什么知识
3. 知识盲区 — 反复查询但未解决的主题
4. 演化趋势 — 频繁修改的知识反映技能成长路径
5. 未言明需求 — 查询了不存在的内容，暗示需要新建知识

设计原则：
- 只分析已有轨迹数据，不产生新数据
- 挖掘结果反向写入知识图谱或建议新建页面
- 与 KnowledgeTrail 共用数据库
- 轻量计算，可定时运行
"""
# Erebus — 黑暗之神 — 暗知识管理，未被显式记录的经验
# 原模块: dark_knowledge.py



import json
import sqlite3
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from core.config import get_config
import logging

logger = logging.getLogger(__name__)


@dataclass
class HiddenAssociation:
    """隐性关联"""
    source_page: str
    target_page: str
    co_occurrence_count: int = 0
    confidence: float = 0.0       # 共现置信度
    typical_context: str = ""     # 典型上下文
    association_type: str = ""    # sequential / contextual / repetitive


@dataclass
class TemporalPattern:
    """时间模式"""
    hour_bucket: str              # morning / afternoon / evening / night
    dominant_form: str = ""       # 该时段主要查询的知识类型
    dominant_domain: str = ""     # 该时段主要领域
    top_pages: List[Tuple[str, int]] = field(default_factory=list)
    pattern_strength: float = 0.0  # 模式强度 0-1


@dataclass
class KnowledgeGap:
    """知识盲区"""
    query_pattern: str            # 查询模式
    frequency: int = 0            # 出现次数
    last_queried: str = ""
    related_existing: List[str] = field(default_factory=list)  # 相关已有知识
    gap_type: str = ""            # unsolved / unrecorded / outdated


@dataclass
class EvolutionSignal:
    """演化信号"""
    page_path: str
    page_title: str = ""
    modification_count: int = 0
    query_growth_rate: float = 0.0   # 查询量增长率
    reference_growth_rate: float = 0.0
    evolution_stage: str = ""         # emerging / growing / mature / fading
    suggested_action: str = ""


class DarkKnowledgeMiner:
    """暗知识挖掘器"""

    def __init__(self, wiki_base: str = None, trail_db_path: str = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.inbox = self.wiki_base / "00-Inbox"
        self.trail_db = Path(trail_db_path) if trail_db_path else (
            self.wiki_base / ".kg" / "trail.db"
        )
        self._has_trail_data = self._check_trail_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.trail_db))
        conn.row_factory = sqlite3.Row
        return conn

    def _check_trail_db(self) -> bool:
        """检查 trail 数据库是否可用"""
        if not self.trail_db.exists():
            return False
        try:
            with self._conn() as conn:
                conn.execute("SELECT 1 FROM trail_events LIMIT 1")
            return True
        except sqlite3.OperationalError:
            return False

    # ========== 隐性关联挖掘 ==========

    def mine_hidden_associations(self, min_confidence: float = 0.3,
                                  days: int = 90) -> List[HiddenAssociation]:
        if not self._has_trail_data:
            return []
        """
        挖掘查询序列中的隐性关联

        算法：同一 session 内连续查询的页面构成关联
        """
        since = (datetime.now() - timedelta(days=days)).isoformat()[:19]

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT session_id, page_path, context, timestamp
                   FROM trail_events
                   WHERE event_type='query' AND timestamp >= ?
                   ORDER BY session_id, timestamp""",
                (since,)
            ).fetchall()

        # 按 session 分组
        sessions = defaultdict(list)
        for row in rows:
            sessions[row["session_id"]].append({
                "page": row["page_path"],
                "context": row["context"] or "",
                "time": row["timestamp"],
            })

        # 统计共现
        pair_counts = Counter()
        pair_contexts = defaultdict(list)

        for session_id, events in sessions.items():
            if len(events) < 2:
                continue

            # 同一 session 内所有相邻对
            for i in range(len(events) - 1):
                a = events[i]["page"]
                b = events[i + 1]["page"]
                if a == b:
                    continue

                pair = tuple(sorted([a, b]))
                pair_counts[pair] += 1
                pair_contexts[pair].append(events[i]["context"])

        # 计算置信度并生成结果
        associations = []
        for (a, b), count in pair_counts.most_common(50):
            if count < 2:
                continue

            # 置信度 = 共现次数 / 两个页面各自的总查询次数
            confidence = self._calculate_co_confidence(a, b, count, since)
            if confidence < min_confidence:
                continue

            # 找出最典型的上下文
            contexts = pair_contexts[(a, b)]
            typical = Counter(contexts).most_common(1)[0][0] if contexts else ""

            associations.append(HiddenAssociation(
                source_page=a,
                target_page=b,
                co_occurrence_count=count,
                confidence=round(confidence, 3),
                typical_context=typical[:100],
                association_type="sequential",
            ))

        associations.sort(key=lambda x: x.confidence, reverse=True)
        return associations

    def _calculate_co_confidence(self, a: str, b: str, co_count: int,
                                  since: str) -> float:
        """计算共现置信度"""
        with self._conn() as conn:
            count_a = conn.execute(
                "SELECT COUNT(*) FROM trail_events WHERE page_path=? AND timestamp >= ?",
                (a, since)
            ).fetchone()[0]
            count_b = conn.execute(
                "SELECT COUNT(*) FROM trail_events WHERE page_path=? AND timestamp >= ?",
                (b, since)
            ).fetchone()[0]

        # Jaccard-like confidence
        union = count_a + count_b - co_count
        return co_count / max(union, 1)

    # ========== 时间模式挖掘 ==========

    def mine_temporal_patterns(self, days: int = 60) -> List[TemporalPattern]:
        """挖掘时间模式"""
        if not self._has_trail_data:
            return []
        since = (datetime.now() - timedelta(days=days)).isoformat()[:19]

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT timestamp, page_path FROM trail_events
                   WHERE event_type='query' AND timestamp >= ?""",
                (since,)
            ).fetchall()

        # 按时段分组
        buckets = {
            "morning": [],    # 06-12
            "afternoon": [],  # 12-18
            "evening": [],    # 18-22
            "night": [],      # 22-06
        }

        for row in rows:
            ts = row["timestamp"]
            try:
                hour = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").hour
            except ValueError:
                hour = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").hour
            if 6 <= hour < 12:
                buckets["morning"].append(row["page_path"])
            elif 12 <= hour < 18:
                buckets["afternoon"].append(row["page_path"])
            elif 18 <= hour < 22:
                buckets["evening"].append(row["page_path"])
            else:
                buckets["night"].append(row["page_path"])

        patterns = []
        for bucket_name, pages in buckets.items():
            if len(pages) < 3:
                continue

            # 获取这些页面的 frontmatter
            forms = []
            domains = []
            for page in set(pages):
                fm = self._get_page_frontmatter(page)
                if fm:
                    forms.append(fm.get("类型", "未知"))
                    domains.append(fm.get("领域", "其他"))

            top_pages = Counter(pages).most_common(5)
            dominant_form = Counter(forms).most_common(1)[0][0] if forms else ""
            dominant_domain = Counter(domains).most_common(1)[0][0] if domains else ""

            # 模式强度 = 该时段最频繁的页面占总查询的比例
            pattern_strength = top_pages[0][1] / max(len(pages), 1) if top_pages else 0

            patterns.append(TemporalPattern(
                hour_bucket=bucket_name,
                dominant_form=dominant_form,
                dominant_domain=dominant_domain,
                top_pages=top_pages,
                pattern_strength=round(pattern_strength, 3),
            ))

        patterns.sort(key=lambda x: x.pattern_strength, reverse=True)
        return patterns

    # ========== 知识盲区挖掘 ==========

    def mine_knowledge_gaps(self, min_frequency: int = 3,
                            days: int = 60) -> List[KnowledgeGap]:
        """
        挖掘知识盲区

        类型：
        - unsolved: 查询了但标记未解决
        - unrecorded: 查询的内容不存在于知识库
        - outdated: 查询了但知识版本过时
        """
        if not self._has_trail_data:
            return []
        since = (datetime.now() - timedelta(days=days)).isoformat()[:19]
        gaps = []

        # 1. 未解决的查询（effect 记录中 success=0）
        with self._conn() as conn:
            unsolved = conn.execute(
                """SELECT page_path, context, COUNT(*) as cnt, MAX(timestamp) as last
                   FROM trail_events
                   WHERE event_type='effect' AND success=0 AND timestamp >= ?
                   GROUP BY page_path
                   HAVING cnt >= ?
                   ORDER BY cnt DESC""",
                (since, min_frequency)
            ).fetchall()

        for row in unsolved:
            gaps.append(KnowledgeGap(
                query_pattern=row["page_path"],
                frequency=row["cnt"],
                last_queried=row["last"],
                gap_type="unsolved",
            ))

        # 2. 高频查询但无 effect 记录（可能知识不够完整）
        with self._conn() as conn:
            no_effect = conn.execute(
                """SELECT page_path, COUNT(*) as cnt, MAX(timestamp) as last
                   FROM trail_events
                   WHERE event_type='query' AND timestamp >= ?
                   GROUP BY page_path
                   HAVING cnt >= ?
                   ORDER BY cnt DESC LIMIT 20""",
                (since, min_frequency * 2)
            ).fetchall()

        for row in no_effect:
            # 检查是否有 effect 记录
            with self._conn() as conn2:
                has_effect = conn2.execute(
                    "SELECT COUNT(*) FROM trail_events WHERE page_path=? AND event_type='effect'",
                    (row["page_path"],)
                ).fetchone()[0]

            if has_effect == 0:
                gaps.append(KnowledgeGap(
                    query_pattern=row["page_path"],
                    frequency=row["cnt"],
                    last_queried=row["last"],
                    gap_type="unrecorded",
                ))

        return gaps

    # ========== 演化信号挖掘 ==========

    def mine_evolution_signals(self, days: int = 60) -> List[EvolutionSignal]:
        """挖掘知识演化信号"""
        if not self._has_trail_data:
            return []
        since = (datetime.now() - timedelta(days=days)).isoformat()[:19]
        half_since = (datetime.now() - timedelta(days=days // 2)).isoformat()[:19]

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT page_path, page_title,
                        total_queries, total_references, total_modifications,
                        first_accessed, last_accessed, effect_score
                   FROM page_stats
                   WHERE last_accessed >= ?
                   ORDER BY total_modifications DESC""",
                (since,)
            ).fetchall()

        signals = []
        for row in rows:
            page = row["page_path"]

            # 计算增长率：后半段 vs 前半段
            with self._conn() as conn2:
                early_queries = conn2.execute(
                    """SELECT COUNT(*) FROM trail_events
                       WHERE page_path=? AND event_type='query'
                       AND timestamp >= ? AND timestamp < ?""",
                    (page, since, half_since)
                ).fetchone()[0]

                late_queries = conn2.execute(
                    """SELECT COUNT(*) FROM trail_events
                       WHERE page_path=? AND event_type='query'
                       AND timestamp >= ?""",
                    (page, half_since)
                ).fetchone()[0]

            query_growth = (late_queries - early_queries) / max(early_queries, 1)

            # 判断演化阶段
            mods = row["total_modifications"] or 0
            queries = row["total_queries"] or 0
            effect = row["effect_score"] or 0

            if mods >= 3 and query_growth > 0.5:
                stage = "emerging"
                action = "知识正在快速演化，建议关注并完善"
            elif queries >= 10 and effect >= 0.7:
                stage = "mature"
                action = "知识成熟，可考虑提取为方法论"
            elif queries >= 5 and effect < 0.3:
                stage = "fading"
                action = "知识效果不佳，可能需要重写或废弃"
            else:
                stage = "stable"
                action = "知识稳定"

            signals.append(EvolutionSignal(
                page_path=page,
                page_title=row["page_title"] or Path(page).stem,
                modification_count=mods,
                query_growth_rate=round(query_growth, 2),
                evolution_stage=stage,
                suggested_action=action,
            ))

        # 按修改次数排序
        signals.sort(key=lambda x: x.modification_count, reverse=True)
        return signals

    # ========== 未言明需求 ==========

    def mine_unrecorded_needs(self, days: int = 30) -> List[Dict]:
        """
        发现未记录的知识需求

        从查询上下文中提取关键词，找出高频出现但无对应页面的主题
        """
        if not self._has_trail_data:
            return []
        since = (datetime.now() - timedelta(days=days)).isoformat()[:19]

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT context FROM trail_events
                   WHERE event_type='query' AND context != '' AND timestamp >= ?""",
                (since,)
            ).fetchall()

        # 提取关键词（简单实现：2-4 字中文词）
        term_counts = Counter()
        for row in rows:
            context = row["context"]
            terms = self._extract_terms(context)
            for term in terms:
                term_counts[term] += 1

        # 过滤掉已有知识页面标题的词汇
        existing_titles = set()
        if self.inbox.exists():
            for page in self.inbox.glob("*.md"):
                existing_titles.add(page.stem)
                # 也提取标题中的关键词
                existing_titles.update(self._extract_terms(page.stem))

        needs = []
        for term, count in term_counts.most_common(30):
            if count < 3:
                continue
            if term in existing_titles or len(term) < 2:
                continue

            needs.append({
                "term": term,
                "frequency": count,
                "suggested_title": f"{term} 笔记",
                "confidence": min(count / 10, 1.0),
            })

        return sorted(needs, key=lambda x: x["frequency"], reverse=True)[:10]

    def _extract_terms(self, text: str) -> List[str]:
        """从文本中提取候选术语"""
        terms = []
        # 提取 2-4 字词
        for i in range(len(text) - 1):
            for length in [4, 3, 2]:
                if i + length <= len(text):
                    substr = text[i:i + length]
                    # 过滤掉纯数字、纯英文短词、常见虚词
                    if self._is_meaningful_term(substr):
                        terms.append(substr)
        return terms

    def _is_meaningful_term(self, term: str) -> bool:
        """判断术语是否有意义"""
        if len(term) < 2:
            return False
        if term.isdigit():
            return False
        # 常见虚词过滤
        stopwords = {"什么", "怎么", "如何", "为什么", "这个", "那个", "一个", "可以",
                     "进行", "使用", "需要", "通过", "作为", "能够", "不能", "没有",
                     "是否", "一下", "一下", "时候", "现在", "今天", "明天"}
        if term in stopwords:
            return False
        # 至少包含一个中文字符
        if not any('一' <= c <= '鿿' for c in term):
            return False
        return True

    # ========== 报告生成 ==========

    def generate_full_report(self) -> str:
        """生成完整的暗知识挖掘报告"""
        associations = self.mine_hidden_associations()
        temporal = self.mine_temporal_patterns()
        gaps = self.mine_knowledge_gaps()
        evolution = self.mine_evolution_signals()
        needs = self.mine_unrecorded_needs()

        lines = [
            "# 暗知识挖掘报告",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d')}",
            "",
            "## 隐性关联",
            f"发现 {len(associations)} 条隐性关联",
            "",
        ]

        for a in associations[:10]:
            name_a = Path(a.source_page).stem
            name_b = Path(a.target_page).stem
            lines.append(f"- **{name_a}** ↔ **{name_b}** "
                       f"(共现 {a.co_occurrence_count} 次, 置信度 {a.confidence})")
            if a.typical_context:
                lines.append(f"  上下文: {a.typical_context[:60]}...")

        lines.extend(["", "## 时间模式", ""])
        for t in temporal:
            lines.append(f"- **{t.hour_bucket}**: 主要查询 {t.dominant_domain}/{t.dominant_form} "
                       f"(模式强度 {t.pattern_strength})")

        lines.extend(["", "## 知识盲区", ""])
        for g in gaps[:10]:
            name = Path(g.query_pattern).stem
            lines.append(f"- **{name}** ({g.gap_type}) — {g.frequency} 次, 最后: {g.last_queried[:10]}")

        lines.extend(["", "## 演化信号", ""])
        for e in evolution[:10]:
            lines.append(f"- **{e.page_title}** ({e.evolution_stage}) — "
                       f"修改 {e.modification_count} 次, {e.suggested_action}")

        lines.extend(["", "## 未记录需求", ""])
        for n in needs[:8]:
            lines.append(f"- **{n['term']}** — {n['frequency']} 次提及, "
                       f"建议新建: {n['suggested_title']}")

        return "\n".join(lines)

    def suggest_new_relations(self, top_n: int = 10) -> List[Dict]:
        """
        建议新建的知识图谱关系

        将高置信度的隐性关联转换为知识图谱关系建议
        """
        associations = self.mine_hidden_associations(min_confidence=0.5)
        suggestions = []

        for a in associations[:top_n]:
            suggestions.append({
                "source": a.source_page,
                "target": a.target_page,
                "relation_type": "related_to",
                "strength": a.confidence,
                "evidence": f"用户在查询中先后访问，共现 {a.co_occurrence_count} 次",
                "source_method": "dark_knowledge_mining",
            })

        return suggestions

    # ========== 辅助方法 ==========

    def _get_page_frontmatter(self, page_path: str) -> Optional[Dict]:
        """获取页面的 frontmatter"""
        try:
            path = Path(page_path)
            if not path.exists():
                # 尝试在 inbox 中查找
                path = self.inbox / Path(page_path).name
            if not path.exists():
                return None

            content = path.read_text(encoding="utf-8")
            return self._extract_frontmatter(content)
        except Exception:
            return None

    @staticmethod
    def _extract_frontmatter(content: str) -> Optional[Dict]:
        import yaml
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    return yaml.safe_load(parts[1]) or {}
                except Exception as e:
                    logger.warning(f"忽略异常: {e}")
        return {}


# ========== 便捷函数 ==========

def mine_dark_knowledge() -> str:
    """便捷函数：运行完整的暗知识挖掘并返回报告"""
    miner = DarkKnowledgeMiner()
    return miner.generate_full_report()


def suggest_relations_from_behavior() -> List[Dict]:
    """便捷函数：从行为中建议新关系"""
    miner = DarkKnowledgeMiner()
    return miner.suggest_new_relations()
