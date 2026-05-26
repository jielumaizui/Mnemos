# -*- coding: utf-8 -*-
"""
ContextAwareQuery — 上下文感知查询

加权排序：
  confidence × 0.4 + topic_relevance × 0.3 + browsing_continuity × 0.2 + time_decay × 0.1
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.config import get_config

logger = logging.getLogger(__name__)


def _get_db_path() -> Path:
    return Path.home() / ".mnemos" / "wiki_state.db"


@dataclass
class QueryResult:
    """查询结果"""
    page_path: str
    title: str
    snippet: str
    score: float
    relevance: float
    confidence: float
    continuity: float
    freshness: float


class ContextAwareQuery:
    """上下文感知查询"""

    QUERY_LOG_TABLE = """
        CREATE TABLE IF NOT EXISTS query_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            context TEXT DEFAULT '',
            result_count INTEGER DEFAULT 0,
            top_score REAL DEFAULT 0,
            created_at TEXT
        )
    """

    WEIGHTS = {
        "confidence": 0.4,
        "topic_relevance": 0.3,
        "browsing_continuity": 0.2,
        "time_decay": 0.1,
    }

    MAX_RESULTS = 10

    def __init__(self, wiki_dir: Path = None):
        self._wiki_dir = wiki_dir or get_config().wiki_dir
        self._db_path = _get_db_path()
        self._init_db()

    def _init_db(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self._db_path), timeout=5) as conn:
            conn.execute(self.QUERY_LOG_TABLE)
            conn.commit()

    def query(self, query_str: str, context: Dict = None) -> List[QueryResult]:
        """执行上下文感知查询

        Args:
            query_str: 查询字符串
            context: 上下文 {working_dir, recent_pages, active_entities}

        Returns:
            排序后的查询结果列表
        """
        context = context or {}
        query_terms = set(self._tokenize(query_str))

        if not query_terms:
            return []

        # 候选页面搜索
        candidates = self._search_pages(query_terms)

        # 计算四维评分
        results = []
        for page_path, title, snippet, match_terms in candidates:
            # confidence: 页面 frontmatter 中的置信度
            confidence = self._get_page_confidence(page_path)

            # topic_relevance: 查询词匹配度
            topic_relevance = len(match_terms & query_terms) / len(query_terms) if query_terms else 0

            # browsing_continuity: 与最近浏览的关联
            continuity = self._compute_continuity(page_path, context)

            # time_decay: 时间衰减
            freshness = self._compute_freshness(page_path)

            # 加权综合分
            score = (
                self.WEIGHTS["confidence"] * confidence +
                self.WEIGHTS["topic_relevance"] * topic_relevance +
                self.WEIGHTS["browsing_continuity"] * continuity +
                self.WEIGHTS["time_decay"] * freshness
            )

            results.append(QueryResult(
                page_path=str(page_path),
                title=title,
                snippet=snippet[:200],
                score=round(score, 3),
                relevance=round(topic_relevance, 3),
                confidence=round(confidence, 3),
                continuity=round(continuity, 3),
                freshness=round(freshness, 3),
            ))

        results.sort(key=lambda r: r.score, reverse=True)

        # 记录查询日志
        self._log_query(query_str, context, len(results),
                        results[0].score if results else 0)

        return results[:self.MAX_RESULTS]

    def _search_pages(self, query_terms: set) -> List[Tuple[Path, str, str, set]]:
        """搜索包含查询词的页面"""
        candidates = []

        for subdir in ["00-Inbox", "01-Projects", "03-Tech", "04-Concepts", "05-MOCs"]:
            md_dir = self._wiki_dir / subdir
            if not md_dir.exists():
                continue
            for md_file in md_dir.glob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8")
                except Exception:
                    continue

                # 跳过 frontmatter
                body = content
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end != -1:
                        body = content[end + 3:]

                body_lower = body.lower()
                matched = set()
                for term in query_terms:
                    if term in body_lower:
                        matched.add(term)

                if not matched:
                    continue

                title = self._extract_title(content) or md_file.stem
                snippet = body.strip()[:300]
                candidates.append((md_file, title, snippet, matched))

        return candidates

    def _compute_continuity(self, page_path: str, context: Dict) -> float:
        """计算浏览连续性（与最近浏览页面的关联度）"""
        recent_pages = context.get("recent_pages", [])
        active_entities = context.get("active_entities", [])

        if not recent_pages and not active_entities:
            return 0.3  # 默认中等

        score = 0.0

        # 最近浏览的页面有 [[链接]] 指向当前页面
        for recent_path in recent_pages[:3]:
            try:
                content = Path(recent_path).read_text(encoding="utf-8")
                page_name = Path(page_path).stem
                if f"[[{page_name}]]" in content or f"[[{page_name}|" in content:
                    score += 0.3
            except Exception:
                continue

        # 活跃实体匹配
        if active_entities:
            try:
                content = Path(page_path).read_text(encoding="utf-8").lower()
                entity_hits = sum(1 for e in active_entities if e.lower() in content)
                score += min(0.4, entity_hits * 0.1)
            except Exception:
                pass

        return min(1.0, score)

    @staticmethod
    def _compute_freshness(page_path) -> float:
        """计算新鲜度（时间衰减）"""
        try:
            mtime = Path(page_path).stat().st_mtime
            age_days = (datetime.now().timestamp() - mtime) / 86400
            # 半衰期 30 天
            return max(0.0, 0.5 ** (age_days / 30.0))
        except Exception:
            return 0.3

    @staticmethod
    def _get_page_confidence(page_path) -> float:
        """从 frontmatter 获取页面置信度"""
        try:
            content = Path(page_path).read_text(encoding="utf-8")[:500]
            m = re.search(r'置信度:\s*([\d.]+)', content)
            if m:
                return float(m.group(1))
            m = re.search(r'confidence:\s*([\d.]+)', content, re.I)
            if m:
                return float(m.group(1))
        except Exception:
            pass
        return 0.5

    @staticmethod
    def _extract_title(content: str) -> str:
        match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        tokens = []
        tokens.extend(w.lower() for w in re.findall(r'[a-zA-Z_]{3,}', text))
        tokens.extend(re.findall(r'[一-龥]{2,4}', text))
        return tokens

    def _log_query(self, query: str, context: Dict, result_count: int,
                   top_score: float):
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                conn.execute(
                    "INSERT INTO query_logs (query, context, result_count, top_score, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (query, json.dumps(context, ensure_ascii=False)[:500],
                     result_count, top_score, datetime.now().isoformat()),
                )
                conn.commit()
        except Exception:
            pass
