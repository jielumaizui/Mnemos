# -*- coding: utf-8 -*-
"""
BehaviorPromptTracker — 画像行为提示使用追踪

职责：
- 记录每次画像行为提示的调用（时间、Agent、来源、A/B 分组、命中策略）
- 提供最近 N 天的效果指标统计

设计原则：
- 写入失败不阻塞主流程
- 策略标签从 prompt 文本中解析，不依赖外部状态
- 可独立使用，也可复用 SignalStore 的 SQLite 连接
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from core.config import get_config
from core.db_utils import delete_older_than

logger = logging.getLogger(__name__)


# 策略标签提取规则：关键字 → 标签
_STRATEGY_PATTERNS = [
    # 能量层
    ("用户专注深度高", "focus_depth_high"),
    ("用户专注深度低", "focus_depth_low"),
    ("用户启动难度高", "startup_difficulty_high"),
    ("用户启动容易", "startup_difficulty_low"),
    ("用户切换弹性高", "switching_flexibility_high"),
    ("用户切换弹性低", "switching_flexibility_low"),
    # 认知层
    ("用户偏抽象思维", "abstraction_high"),
    ("用户偏具象思维", "abstraction_low"),
    ("用户偏好系统视角", "system_view_high"),
    ("用户偏好单点视角", "system_view_low"),
    ("用户质疑倾向强", "skepticism_high"),
    ("用户信任倾向强", "skepticism_low"),
    # 价值层
    ("用户重视正确性", "correctness_first"),
    ("用户重视效率", "efficiency_first"),
    ("用户追求完美", "perfection_oriented"),
    ("用户追求完成", "completion_oriented"),
    ("用户偏好深度", "depth_first"),
    ("用户偏好广度", "breadth_first"),
    ("用户偏行动优先", "action_oriented"),
    ("用户偏分析优先", "analysis_oriented"),
    ("用户偏自主", "autonomous"),
    ("用户偏协作", "collaborative"),
]


def _extract_strategies(prompt_text: str) -> List[str]:
    """从行为提示文本中提取命中的策略标签。"""
    if not prompt_text:
        return []
    strategies = []
    for keyword, tag in _STRATEGY_PATTERNS:
        if keyword in prompt_text and tag not in strategies:
            strategies.append(tag)
    return strategies


def _default_db_path() -> Path:
    """默认使用 SignalStore 的数据库路径。"""
    try:
        return Path(get_config().database_dir) / "user_signals.db"
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
        return Path.home() / ".mnemos" / "user_signals.db"


class BehaviorPromptTracker:
    """画像行为提示使用追踪器"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_table()

    def _ensure_table(self):
        """确保表和索引存在。"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS behavior_prompt_signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        agent TEXT NOT NULL,
                        source TEXT NOT NULL,
                        ab_test_group TEXT,
                        strategies_json TEXT,
                        prompt_length INTEGER DEFAULT 0,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_behavior_prompt_time
                        ON behavior_prompt_signals(timestamp);
                    CREATE INDEX IF NOT EXISTS idx_behavior_prompt_agent
                        ON behavior_prompt_signals(agent);
                    CREATE INDEX IF NOT EXISTS idx_behavior_prompt_source
                        ON behavior_prompt_signals(source);
                    """)
                conn.commit()
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.debug("[BehaviorPromptTracker] 确保表失败", exc_info=True)

    def track(
        self,
        agent: str,
        source: str,
        prompt_text: str,
        ab_test_group: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> bool:
        """
        记录一次行为提示使用。

        Returns:
            是否写入成功
        """
        try:
            strategies = _extract_strategies(prompt_text)
            ts = timestamp or datetime.now().isoformat()
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    """
                    INSERT INTO behavior_prompt_signals
                        (timestamp, agent, source, ab_test_group, strategies_json, prompt_length)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        agent or "unknown",
                        source or "unknown",
                        ab_test_group,
                        json.dumps(strategies, ensure_ascii=False),
                        len(prompt_text) if prompt_text else 0,
                    ),
                )
                conn.commit()
            return True
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.debug("[BehaviorPromptTracker] 写入失败", exc_info=True)
            return False

    def get_metrics(self, days: int = 30) -> Dict:
        """
        获取最近 N 天的行为提示使用指标。

        Returns:
            {
                "days": int,
                "total_calls": int,
                "by_agent": Dict[str, int],
                "by_source": Dict[str, int],
                "by_strategy": Dict[str, int],
                "ab_test": Dict[str, int],
                "daily_calls": List[{"date": str, "count": int}],
            }
        """
        try:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.row_factory = sqlite3.Row  # noqa

                total = conn.execute(
                    "SELECT COUNT(*) FROM behavior_prompt_signals WHERE timestamp >= ?",
                    (cutoff,),
                ).fetchone()[0]

                by_agent = self._aggregate_counts(
                    conn,
                    """
                    SELECT agent, COUNT(*) AS cnt
                    FROM behavior_prompt_signals
                    WHERE timestamp >= ?
                    GROUP BY agent
                    ORDER BY cnt DESC
                    """,
                    (cutoff,),
                )

                by_source = self._aggregate_counts(
                    conn,
                    """
                    SELECT source, COUNT(*) AS cnt
                    FROM behavior_prompt_signals
                    WHERE timestamp >= ?
                    GROUP BY source
                    ORDER BY cnt DESC
                    """,
                    (cutoff,),
                )

                ab_test = self._aggregate_counts(
                    conn,
                    """
                    SELECT COALESCE(ab_test_group, 'unknown') AS grp, COUNT(*) AS cnt
                    FROM behavior_prompt_signals
                    WHERE timestamp >= ?
                    GROUP BY grp
                    ORDER BY cnt DESC
                    """,
                    (cutoff,),
                )

                daily = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT DATE(timestamp) AS date, COUNT(*) AS count
                        FROM behavior_prompt_signals
                        WHERE timestamp >= ?
                        GROUP BY DATE(timestamp)
                        ORDER BY date
                        """,
                        (cutoff,),
                    ).fetchall()
                ]

            by_strategy = self._aggregate_strategies(conn, cutoff)

            return {
                "days": days,
                "total_calls": total,
                "by_agent": by_agent,
                "by_source": by_source,
                "by_strategy": by_strategy,
                "ab_test": ab_test,
                "daily_calls": daily,
            }
        except (OSError, ValueError, TypeError, sqlite3.Error) as e:
            logger.warning("[BehaviorPromptTracker] 获取指标失败: %s", e)
            return {
                "days": days,
                "total_calls": 0,
                "by_agent": {},
                "by_source": {},
                "by_strategy": {},
                "ab_test": {},
                "daily_calls": [],
                "error": str(e),
            }

    @staticmethod
    def _aggregate_counts(conn, sql: str, params: tuple) -> Dict[str, int]:
        result = {}
        for row in conn.execute(sql, params).fetchall():
            key = row[0] or "unknown"
            result[key] = row[1]
        return result

    def cleanup_older_than(self, days: int, dry_run: bool = False) -> int:
        """清理/统计 timestamp 早于保留期限的行为提示信号。"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                return delete_older_than(conn, "behavior_prompt_signals", "timestamp", days, dry_run=dry_run)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.debug("[BehaviorPromptTracker] 清理过期信号失败", exc_info=True)
            return 0

    def _aggregate_strategies(self, conn, cutoff: str) -> Dict[str, int]:
        """聚合 strategies_json 中的策略标签。"""
        result: Dict[str, int] = {}
        try:
            rows = conn.execute(
                "SELECT strategies_json FROM behavior_prompt_signals WHERE timestamp >= ?",
                (cutoff,),
            ).fetchall()
            for (raw,) in rows:
                if not raw:
                    continue
                try:
                    strategies = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(strategies, list):
                    continue
                for tag in strategies:
                    result[tag] = result.get(tag, 0) + 1
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.debug("[BehaviorPromptTracker] 聚合策略失败", exc_info=True)
        return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))
