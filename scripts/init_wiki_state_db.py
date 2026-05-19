#!/usr/bin/env python3
"""
SQLite 全局状态库初始化
统一所有模块的状态追踪、去重索引和处理进度
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.config import get_config

DB_PATH = get_config().claude_data_dir / "wiki_state.db"


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
        cursor = conn.cursor()

        # 1. 行级增量同步位置(模块 A: INGEST)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_positions (
                session_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                synced_lines INTEGER DEFAULT 0,
                last_sync_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. 已处理 session(模块 B: DISTILL)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_sessions (
                session_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                message_count INTEGER DEFAULT 0,
                quality_score REAL DEFAULT 0,
                l2_memos_uid TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                distill_method TEXT DEFAULT 'rule'
            )
        """)

        # 3. session 完成状态(5分钟无修改则完成)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_status (
                session_id TEXT PRIMARY KEY,
                file_path TEXT,
                total_lines INTEGER,
                last_modified TIMESTAMP,
                completed BOOLEAN DEFAULT FALSE,
                completion_detected_at TIMESTAMP
            )
        """)

        # 4. Wiki 页面索引
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wiki_pages (
                page_id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                title TEXT,
                type TEXT,
                source_session TEXT,
                heat_score REAL DEFAULT 0,
                freshness_score REAL DEFAULT 1.0,
                version INTEGER DEFAULT 1,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 5. 实体索引
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                name TEXT PRIMARY KEY,
                type TEXT,
                related_sessions TEXT,
                heat REAL DEFAULT 0,
                first_seen TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 6. 知识单元
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_units (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                type TEXT,
                content_hash TEXT UNIQUE,
                confidence REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 7. 调度任务
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                task_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                subtype TEXT,
                due_date TIMESTAMP,
                reminded BOOLEAN DEFAULT FALSE,
                context TEXT,
                is_periodic BOOLEAN DEFAULT FALSE,
                period TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 8. 健康问题(模块 E: HEALTH)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS health_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_type TEXT NOT NULL,
                page_id TEXT,
                description TEXT,
                severity TEXT DEFAULT 'medium',
                auto_fixed BOOLEAN DEFAULT FALSE,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP
            )
        """)

        # 9. 去重指纹(全局)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ingest_fingerprints (
                fingerprint TEXT PRIMARY KEY,
                session_id TEXT,
                source TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_proc_session ON processed_sessions(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wiki_type ON wiki_pages(type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wiki_status ON wiki_pages(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_entity_type ON entities(type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_health_type ON health_issues(issue_type)")

        conn.commit()
        print(f"[WikiState] 数据库已初始化: {DB_PATH}")


if __name__ == "__main__":
    init_db()
