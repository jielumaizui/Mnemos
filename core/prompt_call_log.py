"""
PromptCallLog — Prompt 调用日志

【E14 全库修复】设计草案占位模块。
记录所有 LLM prompt 调用历史。
"""
import sqlite3
from datetime import datetime
from typing import Dict
from pathlib import Path


class PromptCallLog:
    """Prompt 调用日志（设计草案，待完善）"""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or Path.home() / ".mnemos" / "prompt_calls.db"
        self._init_db()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prompt_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_hash TEXT,
                    model TEXT,
                    tokens_in INTEGER,
                    tokens_out INTEGER,
                    created_at TEXT
                )
            """)
            conn.commit()

    def log(self, entry: Dict):
        """记录一次调用"""
        pass
