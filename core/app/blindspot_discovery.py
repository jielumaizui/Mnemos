# -*- coding: utf-8 -*-
"""
BlindspotDiscovery — 盲点主动发现

搜索时 + 每周总结，带冷却期。
状态机：detected → reminded → investigating → resolved / mitigated / ignored
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class BlindSpotReminder:
    """盲点提醒"""
    topic: str
    description: str
    confidence: float
    status: str  # detected / reminded / investigating / resolved / mitigated / ignored
    detected_at: str
    reminded_at: Optional[str] = None

    @property
    def is_actionable(self) -> bool:
        return self.status in ("detected", "reminded")


class BlindspotDiscovery:
    """盲点主动发现"""

    COOLDOWN_SEC = 86400  # 24 小时冷却
    IGNORE_COOLDOWN_SEC = 604800  # 忽略后 7 天冷却
    MAX_DAILY_REMINDERS = 1  # 每天最多 1 条即时提醒

    def __init__(self, wiki_base: Optional[str] = None, db_path: Optional[str] = None):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            from core.config import get_config
            self.wiki_base = get_config().wiki_dir

        if db_path:
            self.DB_PATH = Path(db_path).expanduser()
        else:
            from core.config import get_config
            self.DB_PATH = get_config().data_dir / "blindspots.db"
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS blindspots (
                    topic TEXT PRIMARY KEY,
                    description TEXT,
                    confidence REAL DEFAULT 0.5,
                    status TEXT DEFAULT 'detected',
                    detected_at TEXT,
                    reminded_at TEXT,
                    last_reminded_at TEXT,
                    resolved_at TEXT
                )
            """)

    def check_blind_spot(self, query: str) -> Optional[BlindSpotReminder]:
        """
        搜索时检查盲点。

        Returns:
            盲点提醒（如果在冷却期内则返回 None）
        """
        blindspots = self._detect_blindspots(query)
        if not blindspots:
            return None

        now = datetime.now()

        for bs in blindspots:
            # 检查冷却期
            if self._is_in_cooldown(bs.topic):
                continue

            # 检查每日上限
            if self._count_today_reminders() >= self.MAX_DAILY_REMINDERS:
                continue

            # 更新状态
            self._update_status(bs.topic, "reminded", now)

            return bs

        return None

    def get_weekly_summary(self) -> List[Dict]:
        """获取本周盲点汇总（供周报使用）"""
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()

        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT topic, description, confidence, status, detected_at, reminded_at
                FROM blindspots
                WHERE detected_at >= ?
                   OR (status IN ('detected', 'reminded', 'investigating') AND detected_at < ?)
                ORDER BY confidence DESC
            """, (week_ago, week_ago))

            return [dict(row) for row in cursor.fetchall()]

    def record_feedback(self, topic: str, action: str) -> None:
        """
        记录用户反馈。

        Args:
            topic: 盲点主题
            action: resolved / mitigated / ignored
        """
        now = datetime.now()
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                UPDATE blindspots
                SET status = ?, resolved_at = ?
                WHERE topic = ?
            """, (action, now.isoformat(), topic))

    def _detect_blindspots(self, query: str) -> List[BlindSpotReminder]:
        """从知识图谱和画像检测盲点"""
        results = []

        # 1. 检查知识空白 — 查询在图谱中无对应实体
        try:
            from core.kia.knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph(wiki_base=str(self.wiki_base))

            keywords = query.lower().split()
            for kw in keywords:
                if len(kw) < 2:
                    continue
                entities = kg.search_entities(kw, limit=1)
                if not entities:
                    # 可能是盲点
                    results.append(BlindSpotReminder(
                        topic=kw,
                        description=f"知识库中缺少关于「{kw}」的记录",
                        confidence=0.4,
                        status="detected",
                        detected_at=datetime.now().isoformat(),
                    ))
        except Exception:
            pass

        # 2. 检查画像盲区
        try:
            from core.persona.hamartia import BlindSpotAnalyzer
            analyzer = BlindSpotAnalyzer()
            profile = analyzer.get_blindspot_profile()
            if profile:
                framing_rigidity = profile.get("framing_rigidity", 0)
                if framing_rigidity > 0.6:
                    results.append(BlindSpotReminder(
                        topic="framing_rigidity",
                        description="可能受问题框架限制，建议多角度审视",
                        confidence=framing_rigidity,
                        status="detected",
                        detected_at=datetime.now().isoformat(),
                    ))
        except Exception:
            pass

        # 保存新发现的盲点
        for bs in results:
            self._upsert_blindspot(bs)

        return results

    def _upsert_blindspot(self, bs: BlindSpotReminder) -> None:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                INSERT OR IGNORE INTO blindspots
                (topic, description, confidence, status, detected_at)
                VALUES (?, ?, ?, ?, ?)
            """, (bs.topic, bs.description, bs.confidence, bs.status, bs.detected_at))

    def _update_status(self, topic: str, status: str, ts: datetime) -> None:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                UPDATE blindspots
                SET status = ?, reminded_at = ?, last_reminded_at = ?
                WHERE topic = ?
            """, (status, ts.isoformat(), ts.isoformat(), topic))

    def _is_in_cooldown(self, topic: str) -> bool:
        now = datetime.now()
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT status, last_reminded_at FROM blindspots WHERE topic = ?",
                (topic,),
            )
            row = cursor.fetchone()
            if not row:
                return False

            status = row[0]
            last_reminded = row[1]

            # 已解决的不提醒
            if status in ("resolved", "mitigated"):
                return True

            # 忽略的 7 天冷却
            if status == "ignored":
                if last_reminded:
                    try:
                        elapsed = (now - datetime.fromisoformat(last_reminded)).total_seconds()
                        if elapsed < self.IGNORE_COOLDOWN_SEC:
                            return True
                    except ValueError:
                        pass
                return False

            # 正常冷却
            if last_reminded:
                try:
                    elapsed = (now - datetime.fromisoformat(last_reminded)).total_seconds()
                    if elapsed < self.COOLDOWN_SEC:
                        return True
                except ValueError:
                    pass

        return False

    def _count_today_reminders(self) -> int:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute("""
                SELECT COUNT(*) FROM blindspots
                WHERE last_reminded_at >= ?
            """, (today_start.isoformat(),))
            return cursor.fetchone()[0]
