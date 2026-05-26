"""
数据库 Schema 初始化 — 统一创建所有缺失的表

【E14 全库修复】文档要求但代码未创建的 21 个表的统一初始化入口。
各模块在首次使用时调用 init_all_tables() 或针对性的 init 函数。
"""

import sqlite3
import logging
from pathlib import Path
from typing import Optional

from core.config import get_config

logger = logging.getLogger(__name__)


def _db_path(db_name: str = "mnemos.db") -> Path:
    """获取主数据库路径"""
    return get_config().data_dir / db_name


def _conn(db_name: str = "mnemos.db") -> sqlite3.Connection:
    """获取数据库连接"""
    db = _db_path(db_name)
    db.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(db))


# ==================== 知识免疫 (E4) ====================

def init_knowledge_immune_tables():
    """创建知识免疫相关表：auto_fix_log, issue_ignore_rules, knowledge_issues"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auto_fix_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id INTEGER,
                fix_type TEXT NOT NULL,
                before_state TEXT,
                after_state TEXT,
                success BOOLEAN DEFAULT 0,
                created_at TEXT NOT NULL,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS issue_ignore_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,
                reason TEXT,
                expires_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id TEXT NOT NULL,
                issue_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'open',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            )
        """)
        conn.commit()
        logger.debug("Knowledge immune tables initialized")


# ==================== 知识图谱 ====================

def init_knowledge_graph_tables():
    """创建知识图谱相关表：co_occurrence_relations, co_occurs, relation_id_map"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS co_occurrence_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_a TEXT NOT NULL,
                entity_b TEXT NOT NULL,
                co_occurrence_count INTEGER DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                UNIQUE(entity_a, entity_b)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS co_occurs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_a TEXT NOT NULL,
                entity_b TEXT NOT NULL,
                context_snippet TEXT,
                source_page TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS relation_id_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                internal_id TEXT UNIQUE NOT NULL,
                external_id TEXT,
                relation_type TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
        logger.debug("Knowledge graph tables initialized")


# ==================== 影子页面 (E3) ====================

def init_shadow_page_tables():
    """创建影子页面相关表：decision_premises"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS decision_premises (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL,
                premise TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                validated BOOLEAN DEFAULT 0,
                validation_result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        logger.debug("Shadow page tables initialized")


# ==================== Embedding 缓存 ====================

def init_embedding_cache_table():
    """创建 Embedding 缓存表"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT UNIQUE NOT NULL,
                embedding BLOB,
                model_version TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_embedding_hash ON embedding_cache(content_hash)
        """)
        conn.commit()
        logger.debug("Embedding cache table initialized")


# ==================== 熵减引擎 (E1) ====================

def init_entropy_tables():
    """创建熵减引擎相关表：entropy_ignored_pairs"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entropy_ignored_pairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_a TEXT NOT NULL,
                entity_b TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(entity_a, entity_b)
            )
        """)
        conn.commit()
        logger.debug("Entropy tables initialized")


# ==================== 文件摄入 ====================

def init_file_index_table():
    """创建文件索引表"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT UNIQUE NOT NULL,
                file_hash TEXT NOT NULL,
                file_size INTEGER,
                mime_type TEXT,
                ingestion_status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        logger.debug("File index table initialized")


# ==================== 画像系统 ====================

def init_persona_tables():
    """创建画像系统相关表：ground_truth_signals, knowledge_profiles"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ground_truth_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_value TEXT,
                confidence REAL DEFAULT 1.0,
                source TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id TEXT UNIQUE NOT NULL,
                profile_data TEXT NOT NULL,
                version INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        logger.debug("Persona tables initialized")


# ==================== KIA 闭环守护 (E12) ====================

def init_guard_tables():
    """创建 KIA 守护相关表：guard_risk_history"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guard_risk_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                risk_type TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                trigger_text TEXT,
                suggestion TEXT,
                contextual_mode TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
        logger.debug("Guard tables initialized")


# ==================== 评分层（ADR-016 / 蓝图V2） ====================

def init_scorer_tables():
    """创建评分层相关表：scorer_models, scorer_training_queue, ground_truth_signals"""
    with _conn() as conn:
        # 模型版本持久化（替代 joblib 文件存储，SQLite 更可控）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scorer_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dimension TEXT NOT NULL,
                model_version TEXT NOT NULL,
                model_type TEXT NOT NULL DEFAULT 'complement_nb',
                model_blob BLOB,
                model_hash TEXT,
                feature_schema_hash TEXT,
                train_samples INTEGER,
                val_auc REAL,
                is_active INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(dimension, model_version)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_models_active ON scorer_models(dimension, is_active)
        """)

        # 延迟标签回填缓冲队列
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scorer_training_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                dimension TEXT NOT NULL,
                features_json TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                earliest_train_at TEXT,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_queue_status ON scorer_training_queue(status, earliest_train_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_queue_dimension ON scorer_training_queue(dimension, status)
        """)

        # 外部真实信号（训练标签来源，禁止自举）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ground_truth_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_value INTEGER NOT NULL,
                confidence REAL DEFAULT 1.0,
                latency_hours INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(session_id, signal_type) ON CONFLICT REPLACE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_gt_session ON ground_truth_signals(session_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_gt_type ON ground_truth_signals(signal_type, created_at)
        """)

        conn.commit()
        logger.debug("Scorer tables initialized (V2 schema with ground_truth_signals)")


# ==================== Skill 飞轮 ====================

def init_skill_tables():
    """创建 Skill 飞轮相关表：skill_deviation_logs, skill_versions"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS skill_deviation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name TEXT NOT NULL,
                expected_output TEXT,
                actual_output TEXT,
                deviation_score REAL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS skill_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name TEXT NOT NULL,
                version TEXT NOT NULL,
                changelog TEXT,
                is_active BOOLEAN DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(skill_name, version)
            )
        """)
        conn.commit()
        logger.debug("Skill tables initialized")


# ==================== 任务分类 ====================

def init_task_classification_table():
    """创建任务分类历史表"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_classification_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                raw_input TEXT,
                classified_type TEXT NOT NULL,
                confidence REAL,
                model_used TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
        logger.debug("Task classification table initialized")


# ==================== 统一入口 ====================

def init_all_tables():
    """初始化所有缺失的数据库表（幂等，可多次调用）"""
    init_knowledge_immune_tables()
    init_knowledge_graph_tables()
    init_shadow_page_tables()
    init_embedding_cache_table()
    init_entropy_tables()
    init_file_index_table()
    init_persona_tables()
    init_guard_tables()
    init_scorer_tables()
    init_skill_tables()
    init_task_classification_table()
    logger.info("All database tables initialized")
