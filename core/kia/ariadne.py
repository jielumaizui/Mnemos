"""
Knowledge Trail - 知识使用轨迹追踪

记录知识的全生命周期使用轨迹：
1. 查询轨迹 — 用户什么时候、在什么上下文查询了知识
2. 引用轨迹 — 知识在哪些对话/文档中被引用
3. 修改轨迹 — 知识页面何时被修改、修改了什么
4. 效果轨迹 — 知识使用后是否解决了问题

洞察输出：
- 热门知识排行（最近 N 天）
- 被遗忘的知识（长期未被访问）
- 用户知识探索路径（从 A 到 B 到 C）
- 知识效果评估（高引用 ≠ 高解决率）

设计原则：
- 轻量记录，不影响主流程性能
- 支持聚合分析，发现使用模式
- 与推送系统联动（热门知识优先推送）
"""
# Ariadne — 阿里阿德涅 — 知识轨迹，线团指引的迷宫之路
# 原模块: knowledge_trail.py



import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from core.config import get_config


@dataclass
class TrailEvent:
    """轨迹事件"""
    event_type: str          # query / reference / modify / effect / push
    page_path: str
    timestamp: str
    session_id: str = ""
    context: str = ""        # 查询/引用的上下文
    source: str = ""         # 事件来源（对话ID、文档路径等）
    quote: str = ""          # 引用的原文片段
    success: bool = None     # 是否解决问题（effect 类型）
    metadata: Dict = field(default_factory=dict)


@dataclass
class PageTrail:
    """单页面轨迹"""
    page_path: str
    page_title: str = ""
    total_queries: int = 0
    total_references: int = 0
    total_modifications: int = 0
    last_accessed: str = ""
    first_accessed: str = ""
    effect_score: float = 0.0   # 0-1，基于 success 记录计算
    events: List[TrailEvent] = field(default_factory=list)


class KnowledgeTrail:
    """知识轨迹追踪器"""

    def __init__(self, wiki_base: str = None, db_path: str = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.inbox = self.wiki_base / "00-Inbox"
        self.db_path = Path(db_path) if db_path else (
            self.wiki_base / ".kg" / "trail.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        schema = """
        CREATE TABLE IF NOT EXISTS trail_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            page_path TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            session_id TEXT,
            context TEXT,
            source TEXT,
            quote TEXT,
            success BOOLEAN,
            metadata TEXT              -- JSON
        );
        CREATE INDEX IF NOT EXISTS idx_trail_page ON trail_events(page_path);
        CREATE INDEX IF NOT EXISTS idx_trail_time ON trail_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_trail_type ON trail_events(event_type);
        CREATE INDEX IF NOT EXISTS idx_trail_session ON trail_events(session_id);

        CREATE TABLE IF NOT EXISTS page_stats (
            page_path TEXT PRIMARY KEY,
            page_title TEXT,
            total_queries INTEGER DEFAULT 0,
            total_references INTEGER DEFAULT 0,
            total_modifications INTEGER DEFAULT 0,
            first_accessed TEXT,
            last_accessed TEXT,
            effect_score REAL DEFAULT 0.0
        );
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(schema)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ========== 事件记录 ==========

    def log_event(self, event: TrailEvent) -> bool:
        """记录轨迹事件"""
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO trail_events
                       (event_type, page_path, timestamp, session_id, context,
                        source, quote, success, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_type, event.page_path, event.timestamp,
                        event.session_id, event.context, event.source,
                        event.quote, event.success,
                        json.dumps(event.metadata, ensure_ascii=False),
                    )
                )
                conn.commit()

                # 更新页面统计
                self._update_page_stats(event.page_path, event.event_type, event.success)
                return True
        except sqlite3.Error:
            return False

    def log_query(self, page_path: str, context: str = "",
                  session_id: str = "", success: bool = None) -> bool:
        """记录查询事件"""
        return self.log_event(TrailEvent(
            event_type="query",
            page_path=page_path,
            timestamp=datetime.now().isoformat()[:19],
            session_id=session_id,
            context=context[:500],
            success=success,
        ))

    def log_reference(self, page_path: str, source: str = "",
                      quote: str = "", session_id: str = "") -> bool:
        """记录引用事件"""
        return self.log_event(TrailEvent(
            event_type="reference",
            page_path=page_path,
            timestamp=datetime.now().isoformat()[:19],
            session_id=session_id,
            source=source,
            quote=quote[:500],
        ))

    def log_modification(self, page_path: str, change_summary: str = "") -> bool:
        """记录修改事件"""
        return self.log_event(TrailEvent(
            event_type="modify",
            page_path=page_path,
            timestamp=datetime.now().isoformat()[:19],
            context=change_summary[:500],
        ))

    def log_effect(self, page_path: str, solved: bool,
                   context: str = "", session_id: str = "") -> bool:
        """记录效果反馈"""
        return self.log_event(TrailEvent(
            event_type="effect",
            page_path=page_path,
            timestamp=datetime.now().isoformat()[:19],
            session_id=session_id,
            context=context[:500],
            success=solved,
        ))

    def _update_page_stats(self, page_path: str, event_type: str,
                           success: bool = None):
        """更新页面统计"""
        with self._conn() as conn:
            # 获取或创建记录
            row = conn.execute(
                "SELECT * FROM page_stats WHERE page_path=?", (page_path,)
            ).fetchone()

            now = datetime.now().isoformat()[:19]

            if row:
                updates = {"last_accessed": now}
                if event_type == "query":
                    updates["total_queries"] = row["total_queries"] + 1
                elif event_type == "reference":
                    updates["total_references"] = row["total_references"] + 1
                elif event_type == "modify":
                    updates["total_modifications"] = row["total_modifications"] + 1

                # 效果分数更新
                if event_type == "effect" and success is not None:
                    # 简单的滑动平均
                    old_score = row["effect_score"] or 0.0
                    old_count = row["total_queries"] or 1
                    new_score = (old_score * (old_count - 1) + (1.0 if success else 0.0)) / old_count
                    updates["effect_score"] = round(new_score, 3)

                set_clause = ", ".join(f"{k}=?" for k in updates)
                conn.execute(
                    f"UPDATE page_stats SET {set_clause} WHERE page_path=?",
                    (*updates.values(), page_path)
                )
            else:
                # 新页面
                title = Path(page_path).stem
                conn.execute(
                    """INSERT INTO page_stats
                       (page_path, page_title, total_queries, total_references,
                        total_modifications, first_accessed, last_accessed, effect_score)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        page_path, title,
                        1 if event_type == "query" else 0,
                        1 if event_type == "reference" else 0,
                        1 if event_type == "modify" else 0,
                        now, now,
                        1.0 if success else 0.0 if success is not None else 0.0,
                    )
                )
            conn.commit()

    # ========== 查询分析 ==========

    def get_page_trail(self, page_path: str, limit: int = 50) -> PageTrail:
        """获取单页面完整轨迹"""
        with self._conn() as conn:
            # 统计
            stats = conn.execute(
                "SELECT * FROM page_stats WHERE page_path=?", (page_path,)
            ).fetchone()

            # 事件
            events = conn.execute(
                """SELECT * FROM trail_events WHERE page_path=?
                   ORDER BY timestamp DESC LIMIT ?""",
                (page_path, limit)
            ).fetchall()

        trail = PageTrail(page_path=page_path)
        if stats:
            trail.page_title = stats["page_title"] or ""
            trail.total_queries = stats["total_queries"] or 0
            trail.total_references = stats["total_references"] or 0
            trail.total_modifications = stats["total_modifications"] or 0
            trail.first_accessed = stats["first_accessed"] or ""
            trail.last_accessed = stats["last_accessed"] or ""
            trail.effect_score = stats["effect_score"] or 0.0

        for row in events:
            trail.events.append(TrailEvent(
                event_type=row["event_type"],
                page_path=row["page_path"],
                timestamp=row["timestamp"],
                session_id=row["session_id"] or "",
                context=row["context"] or "",
                source=row["source"] or "",
                quote=row["quote"] or "",
                success=row["success"],
                metadata=json.loads(row["metadata"] or "{}"),
            ))

        return trail

    def get_popular_pages(self, days: int = 30, top_n: int = 10) -> List[Dict]:
        """获取热门知识排行"""
        since = (datetime.now() - timedelta(days=days)).isoformat()[:19]

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT
                    t.page_path,
                    COALESCE(s.page_title, t.page_path) as page_title,
                    COUNT(*) as event_count,
                    SUM(CASE WHEN t.event_type='query' THEN 1 ELSE 0 END) as query_count,
                    SUM(CASE WHEN t.event_type='reference' THEN 1 ELSE 0 END) as ref_count
                   FROM trail_events t
                   LEFT JOIN page_stats s ON t.page_path = s.page_path
                   WHERE t.timestamp >= ?
                   GROUP BY t.page_path
                   ORDER BY event_count DESC
                   LIMIT ?""",
                (since, top_n)
            ).fetchall()

        return [
            {
                "page_path": row["page_path"],
                "page_title": row["page_title"],
                "event_count": row["event_count"],
                "query_count": row["query_count"],
                "reference_count": row["ref_count"],
            }
            for row in rows
        ]

    def get_forgotten_pages(self, days: int = 30,
                            min_age_days: int = 7) -> List[Dict]:
        """
        获取被遗忘的知识

        条件：
        - 创建超过 min_age_days 天
        - 最近 days 天内没有被查询/引用
        """
        since = (datetime.now() - timedelta(days=days)).isoformat()[:19]
        age_threshold = (datetime.now() - timedelta(days=min_age_days)).isoformat()[:19]

        with self._conn() as conn:
            # 最近活跃的知识
            active_pages = set(
                row[0] for row in conn.execute(
                    "SELECT DISTINCT page_path FROM trail_events WHERE timestamp >= ?",
                    (since,)
                ).fetchall()
            )

            # 所有有一定历史的知识
            all_pages = conn.execute(
                """SELECT page_path, page_title, first_accessed, effect_score
                   FROM page_stats
                   WHERE first_accessed <= ?""",
                (age_threshold,)
            ).fetchall()

        forgotten = []
        for row in all_pages:
            if row["page_path"] not in active_pages:
                forgotten.append({
                    "page_path": row["page_path"],
                    "page_title": row["page_title"],
                    "first_accessed": row["first_accessed"],
                    "effect_score": row["effect_score"],
                    "last_accessed": self._get_last_access(row["page_path"]),
                })

        # 按效果分数排序（效果好的知识被遗忘更可惜）
        forgotten.sort(key=lambda x: x["effect_score"], reverse=True)
        return forgotten

    def get_user_journey(self, session_id: str = None,
                         hours: int = 24) -> List[Dict]:
        """
        获取用户知识探索路径

        Returns:
            按时间排序的知识访问序列
        """
        since = (datetime.now() - timedelta(hours=hours)).isoformat()[:19]

        with self._conn() as conn:
            if session_id:
                rows = conn.execute(
                    """SELECT page_path, event_type, timestamp, context
                       FROM trail_events
                       WHERE session_id=? AND timestamp >= ?
                       ORDER BY timestamp""",
                    (session_id, since)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT page_path, event_type, timestamp, context
                       FROM trail_events
                       WHERE timestamp >= ?
                       ORDER BY timestamp""",
                    (since,)
                ).fetchall()

        return [
            {
                "page_path": row["page_path"],
                "event_type": row["event_type"],
                "timestamp": row["timestamp"],
                "context": row["context"],
            }
            for row in rows
        ]

    def get_effect_report(self, days: int = 30) -> Dict:
        """获取知识效果报告"""
        since = (datetime.now() - timedelta(days=days)).isoformat()[:19]

        with self._conn() as conn:
            total_effects = conn.execute(
                "SELECT COUNT(*) FROM trail_events WHERE event_type='effect' AND timestamp >= ?",
                (since,)
            ).fetchone()[0]

            solved = conn.execute(
                "SELECT COUNT(*) FROM trail_events WHERE event_type='effect' AND success=1 AND timestamp >= ?",
                (since,)
            ).fetchone()[0]

            # 效果最好的知识
            top_effective = conn.execute(
                """SELECT page_path, page_title, effect_score
                   FROM page_stats
                   WHERE effect_score > 0
                   ORDER BY effect_score DESC
                   LIMIT 5"""
            ).fetchall()

            # 效果最差的知识（被查询但很少解决问题）
            least_effective = conn.execute(
                """SELECT page_path, page_title, effect_score, total_queries
                   FROM page_stats
                   WHERE total_queries >= 3 AND effect_score < 0.5
                   ORDER BY effect_score ASC
                   LIMIT 5"""
            ).fetchall()

        return {
            "period_days": days,
            "total_effect_records": total_effects,
            "solved_count": solved,
            "solve_rate": round(solved / max(total_effects, 1), 3),
            "top_effective": [
                {"page": r[0], "title": r[1], "score": r[2]}
                for r in top_effective
            ],
            "needs_improvement": [
                {"page": r[0], "title": r[1], "score": r[2], "queries": r[3]}
                for r in least_effective
            ],
        }

    # ========== 辅助方法 ==========

    def _get_last_access(self, page_path: str) -> str:
        """获取页面最后访问时间"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(timestamp) FROM trail_events WHERE page_path=?",
                (page_path,)
            ).fetchone()
        return row[0] if row and row[0] else ""

    def generate_weekly_report(self) -> str:
        """生成周报"""
        popular = self.get_popular_pages(days=7, top_n=5)
        forgotten = self.get_forgotten_pages(days=7)[:5]
        effect = self.get_effect_report(days=7)

        lines = [
            "# 知识使用周报",
            f"周期: {datetime.now().strftime('%Y-%m-%d')}",
            "",
            "## 热门知识",
            "",
        ]
        for i, p in enumerate(popular, 1):
            lines.append(f"{i}. **{p['page_title']}** — {p['event_count']} 次访问")

        lines.extend(["", "## 被遗忘的知识", ""])
        if forgotten:
            for p in forgotten:
                lines.append(f"- {p['page_title']}（效果分 {p['effect_score']:.1f}）")
        else:
            lines.append("无")

        lines.extend(["", "## 效果统计", ""])
        lines.append(f"- 记录数: {effect['total_effect_records']}")
        lines.append(f"- 解决率: {effect['solve_rate']:.0%}")

        if effect["needs_improvement"]:
            lines.extend(["", "## 需要改进的知识", ""])
            for p in effect["needs_improvement"]:
                lines.append(f"- {p['title']}（效果分 {p['score']:.1f}，被查询 {p['queries']} 次）")

        return "\n".join(lines)


# ========== 便捷函数 ==========

def log_knowledge_usage(page_path: str, event_type: str = "query",
                        context: str = "") -> bool:
    """便捷函数：记录知识使用"""
    trail = KnowledgeTrail()
    if event_type == "query":
        return trail.log_query(page_path, context)
    elif event_type == "reference":
        return trail.log_reference(page_path, context=context)
    elif event_type == "effect":
        return trail.log_effect(page_path, solved=True, context=context)
    return False
