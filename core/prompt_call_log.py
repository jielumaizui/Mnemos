"""
PromptCallLog — Prompt 调用日志与成本监控

【E14 全库修复】记录每次 LLM 调用的成本、质量、耗时，支撑 A/B 测试和模板优化。
"""

import sqlite3
import hashlib
import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PromptCallLog:
    """Prompt 调用日志与成本监控面板"""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or Path.home() / ".mnemos" / "prompt_calls.db"
        self._init_db()

    def _init_db(self):
        """初始化数据库（幂等）"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prompt_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL,
                    session_id TEXT,
                    agent_type TEXT,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    latency_ms INTEGER DEFAULT 0,
                    parse_success BOOLEAN DEFAULT 0,
                    created_at TEXT NOT NULL,
                    template_version TEXT,
                    prompt_hash TEXT,
                    model_name TEXT,
                    error_message TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_prompt_calls_task_type
                ON prompt_calls(task_type)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_prompt_calls_created_at
                ON prompt_calls(created_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_prompt_calls_session
                ON prompt_calls(session_id)
            """)
            conn.commit()

    def _hash_prompt(self, prompt: str) -> str:
        """生成 prompt 哈希"""
        return hashlib.sha256(prompt.encode()).hexdigest()[:16]

    def log(self, task_type: str, session_id: str = None,
            agent_type: str = "claude", prompt: str = "",
            prompt_tokens: int = 0, completion_tokens: int = 0,
            latency_ms: int = 0, parse_success: bool = True,
            template_version: str = None, model_name: str = None,
            error_message: str = None) -> int:
        """
        记录一次 LLM 调用

        Returns:
            记录 ID
        """
        total_tokens = prompt_tokens + completion_tokens
        prompt_hash = self._hash_prompt(prompt) if prompt else None
        created_at = datetime.now().isoformat()

        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.execute("""
                INSERT INTO prompt_calls
                (task_type, session_id, agent_type, prompt_tokens, completion_tokens,
                 total_tokens, latency_ms, parse_success, created_at,
                 template_version, prompt_hash, model_name, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (task_type, session_id, agent_type, prompt_tokens,
                  completion_tokens, total_tokens, latency_ms,
                  int(parse_success), created_at, template_version,
                  prompt_hash, model_name, error_message))
            conn.commit()
            return cursor.lastrowid

    def log_with_timing(self, task_type: str, session_id: str = None,
                        agent_type: str = "claude", prompt: str = "",
                        prompt_tokens: int = 0, completion_tokens: int = 0,
                        parse_success: bool = True, template_version: str = None,
                        model_name: str = None, latency_ms: int = None):
        """带计时的便捷记录方法（如果 latency_ms 未提供则自动记录当前时间）"""
        return self.log(
            task_type=task_type, session_id=session_id,
            agent_type=agent_type, prompt=prompt,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            latency_ms=latency_ms or 0, parse_success=parse_success,
            template_version=template_version, model_name=model_name,
        )

    def get_stats(self, days: int = 7) -> Dict:
        """
        获取最近 N 天的调用统计

        Returns:
            {
                "total_calls": int,
                "total_tokens": int,
                "avg_latency_ms": float,
                "parse_success_rate": float,
                "by_task_type": {task_type: {calls, tokens, avg_latency}},
            }
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # 总体统计
            row = conn.execute("""
                SELECT COUNT(*) as calls,
                       COALESCE(SUM(total_tokens), 0) as tokens,
                       COALESCE(AVG(latency_ms), 0) as avg_latency,
                       COALESCE(AVG(parse_success), 0) as success_rate
                FROM prompt_calls
                WHERE created_at > ?
            """, (cutoff,)).fetchone()

            # 按 task_type 分组
            type_rows = conn.execute("""
                SELECT task_type,
                       COUNT(*) as calls,
                       COALESCE(SUM(total_tokens), 0) as tokens,
                       COALESCE(AVG(latency_ms), 0) as avg_latency
                FROM prompt_calls
                WHERE created_at > ?
                GROUP BY task_type
            """, (cutoff,)).fetchall()

            by_type = {}
            for r in type_rows:
                by_type[r["task_type"]] = {
                    "calls": r["calls"],
                    "tokens": r["tokens"],
                    "avg_latency_ms": round(r["avg_latency"], 1),
                }

            return {
                "period_days": days,
                "total_calls": row["calls"],
                "total_tokens": row["tokens"],
                "avg_latency_ms": round(row["avg_latency"], 1),
                "parse_success_rate": round(row["success_rate"], 3),
                "by_task_type": by_type,
            }

    def get_by_task_type(self, task_type: str, limit: int = 100) -> List[Dict]:
        """获取指定 task_type 的调用记录"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM prompt_calls
                WHERE task_type = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (task_type, limit)).fetchall()
            return [dict(r) for r in rows]

    def get_cost_summary(self, days: int = 30,
                         cost_per_1k_prompt: float = 0.003,
                         cost_per_1k_completion: float = 0.015) -> Dict:
        """
        计算成本汇总（默认按 Claude 3.5 Sonnet 价格）

        Args:
            days: 统计天数
            cost_per_1k_prompt: 每 1K prompt tokens 价格（USD）
            cost_per_1k_completion: 每 1K completion tokens 价格（USD）
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute("""
                SELECT COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) as completion_tokens
                FROM prompt_calls
                WHERE created_at > ?
            """, (cutoff,)).fetchone()

            prompt_tokens = row[0] or 0
            completion_tokens = row[1] or 0
            prompt_cost = (prompt_tokens / 1000) * cost_per_1k_prompt
            completion_cost = (completion_tokens / 1000) * cost_per_1k_completion

            return {
                "period_days": days,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "prompt_cost_usd": round(prompt_cost, 4),
                "completion_cost_usd": round(completion_cost, 4),
                "total_cost_usd": round(prompt_cost + completion_cost, 4),
            }

    def get_latency_summary(self, days: int = 7) -> Dict:
        """获取延迟统计"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute("""
                SELECT MIN(latency_ms) as min_ms,
                       MAX(latency_ms) as max_ms,
                       AVG(latency_ms) as avg_ms,
                       COUNT(*) as calls
                FROM prompt_calls
                WHERE created_at > ? AND latency_ms > 0
            """, (cutoff,)).fetchone()

            return {
                "period_days": days,
                "calls": row[3] or 0,
                "min_ms": row[0] or 0,
                "max_ms": row[1] or 0,
                "avg_ms": round(row[2] or 0, 1),
                "p95_ms": self._get_percentile("latency_ms", 0.95, cutoff),
            }

    def _get_percentile(self, column: str, percentile: float,
                        cutoff: str) -> float:
        """计算百分位数（使用近似方法）"""
        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(f"""
                SELECT {column} FROM prompt_calls
                WHERE created_at > ? AND {column} > 0
                ORDER BY {column}
            """, (cutoff,)).fetchall()

            if not rows:
                return 0.0

            values = [r[0] for r in rows]
            idx = int(len(values) * percentile)
            idx = min(idx, len(values) - 1)
            return float(values[idx])

    def cleanup_old(self, days: int = 90) -> int:
        """清理 N 天前的旧记录"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.execute("""
                DELETE FROM prompt_calls WHERE created_at < ?
            """, (cutoff,))
            conn.commit()
            return cursor.rowcount
