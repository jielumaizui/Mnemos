"""
OutcomeCollector — 结果信号采集器

【E14 全库修复】收集蒸馏产出的实际效果信号，作为贝叶斯权重学习的反馈数据。
信号来源：Obsidian 访问日志、搜索日志、用户反馈、引用计数。
"""

import json
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class OutcomeCollector:
    """采集蒸馏结果的反馈信号"""

    # 信号权重配置
    SIGNAL_WEIGHTS = {
        "viewed": 1.0,        # 用户查看了页面
        "searched": 0.8,      # 页面在搜索中被命中
        "referenced": 1.5,    # 页面被其他页面引用
        "edited": 2.0,        # 用户编辑了页面
        "shared": 1.2,        # 页面被分享
        "ignored_7d": -0.5,   # 7天内未被查看
        "ignored_30d": -1.0,  # 30天内未被查看
    }

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or Path.home() / ".mnemos" / "outcomes.db"
        self._init_db()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    page_id TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    signal_value REAL DEFAULT 0,
                    context TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_outcomes_page
                ON outcomes(page_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_outcomes_signal
                ON outcomes(signal_type)
            """)
            conn.commit()

    def record(self, page_id: str, signal_type: str,
               signal_value: float = None, context: Dict = None):
        """记录一个结果信号"""
        if signal_value is None:
            signal_value = self.SIGNAL_WEIGHTS.get(signal_type, 0.0)

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT INTO outcomes (page_id, signal_type, signal_value, context, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (page_id, signal_type, signal_value,
                  json.dumps(context or {}, ensure_ascii=False),
                  datetime.now().isoformat()))
            conn.commit()

    def record_view(self, page_id: str, source: str = "obsidian"):
        """记录页面被查看"""
        self.record(page_id, "viewed", context={"source": source})

    def record_reference(self, from_page: str, to_page: str):
        """记录页面间引用"""
        self.record(to_page, "referenced",
                    signal_value=self.SIGNAL_WEIGHTS["referenced"],
                    context={"from_page": from_page})

    def record_search_hit(self, page_id: str, query: str):
        """记录搜索命中"""
        self.record(page_id, "searched",
                    context={"query": query})

    def record_edit(self, page_id: str, edit_size: int = 0):
        """记录页面被编辑"""
        self.record(page_id, "edited",
                    signal_value=self.SIGNAL_WEIGHTS["edited"],
                    context={"edit_size": edit_size})

    def get_page_score(self, page_id: str, days: int = 30) -> float:
        """
        计算页面在最近 N 天的综合效果分

        Returns:
            综合分数（可正可负）
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute("""
                SELECT signal_type, signal_value, created_at
                FROM outcomes
                WHERE page_id = ? AND created_at > ?
            """, (page_id, cutoff)).fetchall()

        if not rows:
            return 0.0

        score = 0.0
        for signal_type, value, created_at in rows:
            # 时间衰减：越近的信号权重越高
            try:
                age_days = (datetime.now() - datetime.fromisoformat(created_at)).days
                decay = max(0.1, 1.0 - age_days / (days * 2))
            except Exception:
                decay = 1.0

            score += (value or self.SIGNAL_WEIGHTS.get(signal_type, 0.0)) * decay

        return round(score, 2)

    def get_top_pages(self, limit: int = 20, days: int = 30) -> List[Dict]:
        """获取效果最好的页面"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT page_id,
                       SUM(signal_value * MAX(0.1, 1.0 - (julianday('now') - julianday(created_at)) / ?)) as score,
                       COUNT(*) as signal_count
                FROM outcomes
                WHERE created_at > ?
                GROUP BY page_id
                ORDER BY score DESC
                LIMIT ?
            """, (days * 2, cutoff, limit)).fetchall()

            return [{"page_id": r["page_id"],
                     "score": round(r["score"], 2),
                     "signal_count": r["signal_count"]} for r in rows]

    def scan_vault_for_signals(self, wiki_dir: Path):
        """
        扫描 Wiki Vault 收集被动信号

        - 统计每个页面的引用次数
        - 检测长时间未编辑的页面
        """
        if not wiki_dir.exists():
            return

        # 收集所有页面和链接
        page_links: Dict[str, List[str]] = {}
        page_mtimes: Dict[str, float] = {}

        for md_file in wiki_dir.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                page_id = md_file.stem
                page_mtimes[page_id] = md_file.stat().st_mtime

                # 提取 [[链接]]
                import re
                links = re.findall(r'\[\[([^\]|]+)', content)
                page_links[page_id] = links
            except Exception:
                continue

        # 记录引用信号
        for from_page, links in page_links.items():
            for to_page in links:
                to_clean = to_page.strip()
                if to_clean in page_mtimes:
                    self.record_reference(from_page, to_clean)

        # 检测长时间未编辑的页面
        now = datetime.now().timestamp()
        for page_id, mtime in page_mtimes.items():
            age_days = (now - mtime) / 86400
            if age_days > 30:
                self.record(page_id, "ignored_30d",
                            signal_value=self.SIGNAL_WEIGHTS["ignored_30d"],
                            context={"age_days": round(age_days, 1)})
            elif age_days > 7:
                self.record(page_id, "ignored_7d",
                            signal_value=self.SIGNAL_WEIGHTS["ignored_7d"],
                            context={"age_days": round(age_days, 1)})

    def get_summary(self, days: int = 7) -> Dict:
        """获取信号汇总"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute("""
                SELECT COUNT(*) as total_signals,
                       COUNT(DISTINCT page_id) as pages_affected,
                       AVG(signal_value) as avg_value
                FROM outcomes
                WHERE created_at > ?
            """, (cutoff,)).fetchone()

            type_rows = conn.execute("""
                SELECT signal_type, COUNT(*) as count
                FROM outcomes
                WHERE created_at > ?
                GROUP BY signal_type
            """, (cutoff,)).fetchall()

        return {
            "period_days": days,
            "total_signals": row[0],
            "pages_affected": row[1],
            "avg_signal_value": round(row[2] or 0, 3),
            "by_type": {r[0]: r[1] for r in type_rows},
        }
