#!/usr/bin/env python3
"""
数据库迁移脚本 — 统一 sync_log.db Schema

将蓝图 15-数据字典/01-数据库Schema总览.md 定义的所有表创建到 ~/.mnemos/sync_log.db。
支持增量迁移：只创建不存在的表/字段，不破坏已有数据。

用法：
    python3 scripts/migrate_db.py              # 执行迁移
    python3 scripts/migrate_db.py --status      # 查看当前 schema 版本
    python3 scripts/migrate_db.py --rollback N   # 回滚到版本 N
"""

import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import get_config

SCHEMA_VERSION = 1
SCHEMA_DESCRIPTION = "v2.0.0 统一 Schema：同步层+评分层+蒸馏层+知识图谱+画像+运维+L3扩展"

# === 所有表的 CREATE 语句 ===
# 每个条目: (table_name, CREATE_SQL)
TABLES = [
    # -- 版本管理 --
    ("schema_version", """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            description TEXT NOT NULL,
            checksum TEXT
        )
    """),

    # -- 同步层 --
    ("sessions", """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY,
            session_uuid TEXT UNIQUE NOT NULL,
            agent_name TEXT NOT NULL,
            model_tag TEXT,
            title TEXT,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            archived BOOLEAN DEFAULT 0
        )
    """),

    ("turns", """
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            turn_index INTEGER NOT NULL,
            user_content TEXT,
            assistant_content TEXT,
            token_count INTEGER,
            timestamp TIMESTAMP
        )
    """),

    ("memories", """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY,
            memos_id INTEGER,
            session_id INTEGER REFERENCES sessions(id),
            content TEXT NOT NULL,
            tags TEXT,
            sensitivity TEXT,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            checksum TEXT
        )
    """),

    ("sync_log", """
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            memos_uids TEXT,
            status TEXT DEFAULT 'synced',
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            distill_status TEXT DEFAULT 'pending',
            distill_job_id TEXT,
            distilled_at TIMESTAMP,
            wiki_page_paths TEXT,
            distill_error TEXT,
            UNIQUE(agent_name, session_id, turn_number)
        )
    """),

    ("sync_queue", """
        CREATE TABLE IF NOT EXISTS sync_queue (
            id INTEGER PRIMARY KEY,
            event_type TEXT NOT NULL,
            session_path TEXT,
            status TEXT DEFAULT 'pending',
            retry_count INTEGER DEFAULT 0,
            created_at TIMESTAMP,
            processed_at TIMESTAMP,
            error_msg TEXT
        )
    """),

    # -- 评分层 --
    ("score_cards", """
        CREATE TABLE IF NOT EXISTS score_cards (
            id INTEGER PRIMARY KEY,
            memory_id INTEGER REFERENCES memories(id),
            rule_prior REAL,
            ml_likelihood REAL,
            posterior REAL,
            confidence REAL,
            features TEXT,
            model_version INTEGER REFERENCES score_models(id),
            scored_at TIMESTAMP,
            latency_ms INTEGER
        )
    """),

    ("score_models", """
        CREATE TABLE IF NOT EXISTS score_models (
            id INTEGER PRIMARY KEY,
            version_tag TEXT UNIQUE,
            algorithm TEXT,
            feature_count INTEGER,
            train_samples INTEGER,
            accuracy REAL,
            model_path TEXT,
            created_at TIMESTAMP,
            is_rolled_back BOOLEAN DEFAULT 0
        )
    """),

    # -- 蒸馏层 --
    ("knowledge_fragments", """
        CREATE TABLE IF NOT EXISTS knowledge_fragments (
            id INTEGER PRIMARY KEY,
            memory_id INTEGER REFERENCES memories(id),
            form TEXT,
            title TEXT,
            frontmatter TEXT,
            temporal_scope TEXT,
            boundaries TEXT,
            anti_patterns TEXT,
            confidence REAL,
            extracted_at TIMESTAMP,
            llm_calls_used INTEGER
        )
    """),

    ("distillation_drafts", """
        CREATE TABLE IF NOT EXISTS distillation_drafts (
            id INTEGER PRIMARY KEY,
            session_id INTEGER REFERENCES sessions(id),
            turn_range TEXT,
            draft_content TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
    """),

    ("recirculation_guard", """
        CREATE TABLE IF NOT EXISTS recirculation_guard (
            content_hash TEXT PRIMARY KEY,
            first_seen_at TIMESTAMP,
            source_type TEXT
        )
    """),

    # -- 蒸馏额外表 --
    ("incremental_drafts", """
        CREATE TABLE IF NOT EXISTS incremental_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_range TEXT,
            title TEXT,
            content TEXT,
            confidence REAL,
            status TEXT DEFAULT 'draft',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """),

    ("cross_agent_links", """
        CREATE TABLE IF NOT EXISTS cross_agent_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_agent TEXT NOT NULL,
            target_agent TEXT NOT NULL,
            link_type TEXT,
            confidence REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """),

    ("knowledge_evolution", """
        CREATE TABLE IF NOT EXISTS knowledge_evolution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_uid TEXT,
            relation_id INTEGER,
            alert_type TEXT,
            detail TEXT,
            suggested_action TEXT,
            resolved BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """),

    ("distill_feedback", """
        CREATE TABLE IF NOT EXISTS distill_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dimension TEXT,
            expected_value REAL,
            actual_value REAL,
            source TEXT,
            context TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """),

    # -- 跨模块关联 --
    ("memos_wiki_link", """
        CREATE TABLE IF NOT EXISTS memos_wiki_link (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memos_uid TEXT NOT NULL,
            wiki_page_path TEXT NOT NULL,
            link_type TEXT DEFAULT 'source',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """),

    ("prompt_call_log", """
        CREATE TABLE IF NOT EXISTS prompt_call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT,
            session_id TEXT,
            agent_type TEXT,
            prompt_tokens INTEGER,
            output_tokens INTEGER,
            elapsed_seconds REAL,
            success BOOLEAN DEFAULT 1,
            error_type TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """),

    # -- 知识图谱 --
    ("entities", """
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            entity_type TEXT,
            aliases TEXT,
            first_seen TIMESTAMP,
            last_updated TIMESTAMP
        )
    """),

    ("entity_aliases", """
        CREATE TABLE IF NOT EXISTS entity_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_uid TEXT NOT NULL,
            alias TEXT NOT NULL,
            UNIQUE(entity_uid, alias)
        )
    """),

    ("relations", """
        CREATE TABLE IF NOT EXISTS relations (
            id INTEGER PRIMARY KEY,
            source_id INTEGER REFERENCES entities(id),
            target_id INTEGER REFERENCES entities(id),
            rel_type TEXT,
            strength REAL,
            confidence REAL,
            evidence TEXT,
            created_at TIMESTAMP,
            is_suspect BOOLEAN DEFAULT 0,
            context TEXT
        )
    """),

    ("relation_context_embeddings", """
        CREATE TABLE IF NOT EXISTS relation_context_embeddings (
            id INTEGER PRIMARY KEY,
            relation_id INTEGER REFERENCES relations(id),
            embedding BLOB,
            model_version TEXT,
            created_at TIMESTAMP
        )
    """),

    ("evolution_alerts", """
        CREATE TABLE IF NOT EXISTS evolution_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_uid TEXT,
            relation_id INTEGER,
            alert_type TEXT,
            detail TEXT,
            resolved BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """),

    ("query_logs", """
        CREATE TABLE IF NOT EXISTS query_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT,
            context_topic TEXT,
            results TEXT,
            user_clicked BOOLEAN,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """),

    # -- 用户画像 --
    ("persona_profiles", """
        CREATE TABLE IF NOT EXISTS persona_profiles (
            id INTEGER PRIMARY KEY,
            profile_type TEXT,
            content TEXT,
            generated_at TIMESTAMP,
            source_count INTEGER
        )
    """),

    ("topic_interests", """
        CREATE TABLE IF NOT EXISTS topic_interests (
            id INTEGER PRIMARY KEY,
            topic TEXT UNIQUE NOT NULL,
            score REAL,
            last_interacted TIMESTAMP,
            interaction_count INTEGER
        )
    """),

    # -- 运维 --
    ("event_log", """
        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            level TEXT,
            component TEXT,
            event_type TEXT,
            message TEXT,
            context TEXT
        )
    """),

    ("feedback_log", """
        CREATE TABLE IF NOT EXISTS feedback_log (
            id INTEGER PRIMARY KEY,
            memory_id INTEGER REFERENCES memories(id),
            feedback_type TEXT,
            rating INTEGER,
            action TEXT,
            timestamp TIMESTAMP
        )
    """),

    ("blind_spots", """
        CREATE TABLE IF NOT EXISTS blind_spots (
            id INTEGER PRIMARY KEY,
            topic TEXT,
            state TEXT DEFAULT 'detected',
            detection_count INTEGER DEFAULT 1,
            first_detected TIMESTAMP,
            last_reminded TIMESTAMP,
            resolved_at TIMESTAMP
        )
    """),

    ("disputes", """
        CREATE TABLE IF NOT EXISTS disputes (
            id INTEGER PRIMARY KEY,
            knowledge_id INTEGER REFERENCES knowledge_fragments(id),
            conflict_type TEXT,
            intensity REAL,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP,
            resolved_at TIMESTAMP
        )
    """),

    # -- L3 扩展 --
    ("bayesian_weights", """
        CREATE TABLE IF NOT EXISTS bayesian_weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            dimension TEXT NOT NULL,
            alpha REAL DEFAULT 1.0,
            beta REAL DEFAULT 1.0,
            UNIQUE(domain, dimension)
        )
    """),

    ("distill_outcomes", """
        CREATE TABLE IF NOT EXISTS distill_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_id TEXT,
            domain TEXT,
            original_scores TEXT,
            signals TEXT,
            quality_score REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """),

    ("weight_switch_log", """
        CREATE TABLE IF NOT EXISTS weight_switch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT,
            switched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sample_count INTEGER,
            reason TEXT
        )
    """),

    ("intent_corrections", """
        CREATE TABLE IF NOT EXISTS intent_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern TEXT NOT NULL,
            full_text TEXT,
            original_intent TEXT NOT NULL,
            corrected_intent TEXT NOT NULL,
            hit_count INTEGER DEFAULT 1,
            last_hit_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(pattern, original_intent)
        )
    """),

    # -- 全文搜索 --
    # FTS5 虚拟表需要单独处理（用 CREATE VIRTUAL TABLE）
]

FTS5_TABLES = [
    ("search_index", """
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
            content,
            tags,
            tokenize='porter unicode61'
        )
    """),
]

# === 索引 ===
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_name, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, turn_index)",
    "CREATE INDEX IF NOT EXISTS idx_memories_checksum ON memories(checksum)",
    "CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_score_cards_memory ON score_cards(memory_id)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_memory ON knowledge_fragments(memory_id)",
    "CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_trace ON event_log(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_time ON event_log(timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_memory ON feedback_log(memory_id)",
    "CREATE INDEX IF NOT EXISTS idx_blind_spot_state ON blind_spots(state)",
    "CREATE INDEX IF NOT EXISTS idx_memos_uid ON memos_wiki_link(memos_uid)",
    "CREATE INDEX IF NOT EXISTS idx_wiki_path ON memos_wiki_link(wiki_page_path)",
    "CREATE INDEX IF NOT EXISTS idx_entity_aliases_alias ON entity_aliases(alias)",
]

# === 触发器 ===
TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS memories_insert AFTER INSERT ON memories
       BEGIN
           INSERT INTO search_index(rowid, content, tags) VALUES (new.id, new.content, new.tags);
       END""",
]


def get_db_path() -> Path:
    return get_config().data_dir / "sync_log.db"


def compute_checksum() -> str:
    all_sql = "".join(sql for _, sql in TABLES + FTS5_TABLES)
    return hashlib.md5(all_sql.encode()).hexdigest()[:12]


def get_current_version(conn: sqlite3.Connection) -> int:
    try:
        cursor = conn.execute("SELECT MAX(version) FROM schema_version")
        row = cursor.fetchone()
        return row[0] if row and row[0] else 0
    except sqlite3.OperationalError:
        return 0


def migrate(db_path: Path = None):
    db_path = db_path or get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")

    current = get_current_version(conn)
    if current >= SCHEMA_VERSION:
        print(f"Schema 已是最新版本 v{current}，无需迁移")
        conn.close()
        return

    print(f"当前版本: v{current}, 目标版本: v{SCHEMA_VERSION}")

    # 备份
    backup_path = db_path.with_suffix(f".db.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}")
    import shutil
    shutil.copy2(str(db_path), str(backup_path))
    print(f"已备份到: {backup_path}")

    try:
        # 创建表
        for table_name, create_sql in TABLES:
            conn.execute(create_sql)
            print(f"  表 {table_name} ✓")

        # FTS5 虚拟表
        for table_name, create_sql in FTS5_TABLES:
            try:
                conn.execute(create_sql)
                print(f"  FTS5 表 {table_name} ✓")
            except sqlite3.OperationalError as e:
                print(f"  FTS5 表 {table_name} 跳过（{e}）")

        # 索引
        for idx_sql in INDEXES:
            conn.execute(idx_sql)
        print(f"  索引创建完成 ({len(INDEXES)} 个)")

        # 触发器
        for trig_sql in TRIGGERS:
            try:
                conn.execute(trig_sql)
            except sqlite3.OperationalError as e:
                print(f"  触发器跳过（{e}）")
        print(f"  触发器创建完成")

        # sync_log 表扩展：检查新字段是否存在，不存在则添加
        _alter_sync_log(conn)

        # 记录版本
        checksum = compute_checksum()
        conn.execute(
            "INSERT INTO schema_version (version, description, checksum) VALUES (?, ?, ?)",
            (SCHEMA_VERSION, SCHEMA_DESCRIPTION, checksum),
        )
        conn.commit()
        print(f"\n迁移完成: v{current} → v{SCHEMA_VERSION}")

    except Exception as e:
        conn.rollback()
        print(f"\n迁移失败，已回滚: {e}")
        print(f"备份文件: {backup_path}")
        raise
    finally:
        conn.close()


def _alter_sync_log(conn: sqlite3.Connection):
    """为已有 sync_log 表添加新字段（如果不存在）"""
    new_columns = {
        "distill_status": "TEXT DEFAULT 'pending'",
        "distill_job_id": "TEXT",
        "distilled_at": "TIMESTAMP",
        "wiki_page_paths": "TEXT",
        "distill_error": "TEXT",
    }
    cursor = conn.execute("PRAGMA table_info(sync_log)")
    existing = {row[1] for row in cursor.fetchall()}
    for col, col_type in new_columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE sync_log ADD COLUMN {col} {col_type}")
            print(f"  sync_log +{col} ✓")


def show_status(db_path: Path = None):
    db_path = db_path or get_db_path()
    if not db_path.exists():
        print(f"数据库不存在: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    current = get_current_version(conn)
    print(f"Schema 版本: v{current}")

    cursor = conn.execute("SELECT version, applied_at, description FROM schema_version ORDER BY version")
    for row in cursor.fetchall():
        print(f"  v{row[0]}: {row[2]} ({row[1]})")

    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    print(f"\n已有表 ({len(tables)}): {', '.join(tables)}")
    conn.close()


def rollback(db_path: Path = None, target_version: int = 0):
    """回滚到指定版本——通过恢复备份实现"""
    print("回滚需要从备份恢复数据库。请手动操作：")
    print(f"1. 找到备份文件: {db_path or get_db_path()}.bak.*")
    print(f"2. 停止所有 Mnemos 进程")
    print(f"3. 替换 {db_path or get_db_path()} 为备份文件")
    print("4. 重启 Mnemos")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Mnemos 数据库迁移")
    parser.add_argument("--status", action="store_true", help="查看当前 Schema 版本")
    parser.add_argument("--rollback", type=int, metavar="VERSION", help="回滚到指定版本")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.rollback is not None:
        rollback(target_version=args.rollback)
    else:
        migrate()
