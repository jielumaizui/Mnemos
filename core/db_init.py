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
# 对齐蓝图：07-问题处理流水线设计.md

def _ensure_column(conn, table: str, col: str, dtype: str, default=None):
    """幂等添加列（SQLite ALTER TABLE ADD COLUMN 兼容）"""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if col not in existing:
        ddl = f"ALTER TABLE {table} ADD COLUMN {col} {dtype}"
        if default is not None:
            ddl += f" DEFAULT {default}"
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as e:
            logger.debug(f"Column {col} on {table}: {e}")


def init_knowledge_immune_tables():
    """创建/迁移知识免疫三表：knowledge_issues, auto_fix_log, issue_ignore_rules

    对齐蓝图 §7.1 / §4.3 / §6.3，保留旧列向后兼容。
    """
    with _conn() as conn:
        # ---- knowledge_issues ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id TEXT,
                source_module TEXT,
                issue_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT DEFAULT 'detected',
                page_path TEXT,
                related_pages TEXT,
                description TEXT,
                suggestion TEXT,
                detected_at TIMESTAMP,
                resolved_at TIMESTAMP,
                resolved_by TEXT,
                resolution_action TEXT,
                resolution_notes TEXT,
                ignore_rule_id TEXT,
                page_id TEXT,
                created_at TEXT,
                UNIQUE(source_module, issue_type, page_path, related_pages)
            )
        """)
        _ensure_column(conn, "knowledge_issues", "issue_id", "TEXT")
        _ensure_column(conn, "knowledge_issues", "source_module", "TEXT")
        _ensure_column(conn, "knowledge_issues", "status", "TEXT", "'detected'")
        _ensure_column(conn, "knowledge_issues", "page_path", "TEXT")
        _ensure_column(conn, "knowledge_issues", "related_pages", "TEXT")
        _ensure_column(conn, "knowledge_issues", "suggestion", "TEXT")
        _ensure_column(conn, "knowledge_issues", "detected_at", "TIMESTAMP")
        _ensure_column(conn, "knowledge_issues", "resolved_by", "TEXT")
        _ensure_column(conn, "knowledge_issues", "resolution_action", "TEXT")
        _ensure_column(conn, "knowledge_issues", "resolution_notes", "TEXT")
        _ensure_column(conn, "knowledge_issues", "ignore_rule_id", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_status ON knowledge_issues(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_severity ON knowledge_issues(severity)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_page ON knowledge_issues(page_path)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_detected ON knowledge_issues(detected_at)")

        # ---- auto_fix_log ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auto_fix_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id TEXT,
                issue_type TEXT,
                page_path TEXT,
                action TEXT,
                success BOOLEAN,
                backup_id TEXT,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fix_type TEXT,
                before_state TEXT,
                after_state TEXT,
                error TEXT
            )
        """)
        _ensure_column(conn, "auto_fix_log", "issue_id", "TEXT")
        _ensure_column(conn, "auto_fix_log", "issue_type", "TEXT")
        _ensure_column(conn, "auto_fix_log", "page_path", "TEXT")
        _ensure_column(conn, "auto_fix_log", "action", "TEXT")
        _ensure_column(conn, "auto_fix_log", "backup_id", "TEXT")
        _ensure_column(conn, "auto_fix_log", "error_message", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fix_log_issue ON auto_fix_log(issue_id)")

        # ---- issue_ignore_rules ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS issue_ignore_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT,
                issue_type TEXT,
                page_pattern TEXT,
                reason TEXT,
                expires_at TIMESTAMP,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                pattern TEXT
            )
        """)
        _ensure_column(conn, "issue_ignore_rules", "rule_id", "TEXT")
        _ensure_column(conn, "issue_ignore_rules", "issue_type", "TEXT")
        _ensure_column(conn, "issue_ignore_rules", "page_pattern", "TEXT")
        _ensure_column(conn, "issue_ignore_rules", "created_by", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ignore_type ON issue_ignore_rules(issue_type)")

        conn.commit()
        logger.debug("Knowledge immune tables initialized & migrated")


# ==================== 知识图谱 ====================

def init_knowledge_graph_tables():
    """创建知识图谱相关表：co_occurrence_relations, co_occurs"""
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
        conn.commit()
        logger.debug("Knowledge graph tables initialized")


# ==================== 影子页面 (E3) ====================
# NOTE: decision_premises 表当前无任何业务代码引用，已移除。
# 若将来需要启用影子页面功能，可从蓝图 01-影子页面.md 恢复 Schema。


# ==================== Embedding 缓存 ====================
# NOTE: embedding_cache 表当前无任何业务代码引用，已移除。
# 若将来需要启用 Embedding 缓存，可恢复 init_embedding_cache_table()。


# ==================== 熵减引擎 (E1) ====================
# NOTE: entropy_ignored_pairs 表当前无任何业务代码引用，已移除。
# 若将来需要启用熵减忽略对功能，可恢复 init_entropy_tables()。


# ==================== 文件摄入 ====================
# NOTE: file_index 表当前无任何业务代码引用，已移除。
# 若将来需要启用文件索引功能，可恢复 init_file_index_table()。


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
# NOTE: guard_risk_history 表当前无任何业务代码引用，已移除。
# 若将来需要启用 KIA 守护功能，可恢复 init_guard_tables()。


# ==================== 评分层（ADR-016 / 蓝图V2） ====================

def init_scorer_tables():
    """创建评分层相关表：scorer_training_queue, ground_truth_signals, scorer_models"""
    with _conn() as conn:
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
        # 迁移：旧版 persona 层的 ground_truth_signals 缺少 session_id 列
        existing = {row[1] for row in conn.execute("PRAGMA table_info(ground_truth_signals)")}
        if "session_id" not in existing:
            conn.execute("ALTER TABLE ground_truth_signals ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_gt_session ON ground_truth_signals(session_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_gt_type ON ground_truth_signals(signal_type, created_at)
        """)

        # 模型版本持久化（ADR-016 V2 评分层）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scorer_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dimension TEXT NOT NULL,
                model_version TEXT NOT NULL,
                model_type TEXT,
                model_blob BLOB,
                model_hash TEXT,
                train_samples INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                meta_json TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sm_dimension ON scorer_models(dimension, is_active, created_at)
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sm_version ON scorer_models(dimension, model_version)
        """)
        # 迁移：旧表可能没有 meta_json
        existing_sm = {row[1] for row in conn.execute("PRAGMA table_info(scorer_models)")}
        if "meta_json" not in existing_sm:
            conn.execute("ALTER TABLE scorer_models ADD COLUMN meta_json TEXT")

        conn.commit()
        logger.debug("Scorer tables initialized (V2 schema with scorer_models)")


# ==================== Skill 飞轮 ====================

def init_skill_tables():
    """创建 Skill 飞轮相关表：skill_versions

    NOTE: skill_deviation_logs 当前无任何业务代码引用，已移除。
    skill_versions 保留（ixion.py 在 flywheel.db 中使用同名表，未来可能统一）。
    """
    with _conn() as conn:
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


# ==================== 知识轨迹 (E23) ====================
# 对齐蓝图：23-知识轨迹.md

def init_trail_tables():
    """创建知识轨迹表：trail_events, page_stats

    NOTE: trail_usage 当前无任何业务代码引用，已移除。
    trail_events / page_stats 保留（ariadne.py 在 trail.db 中使用同名表，未来可能统一）。
    """
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trail_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                page_path TEXT,
                details TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS page_stats (
                page_path TEXT PRIMARY KEY,
                read_count INTEGER DEFAULT 0,
                quote_count INTEGER DEFAULT 0,
                modify_count INTEGER DEFAULT 0,
                solve_count INTEGER DEFAULT 0,
                last_access TIMESTAMP
            )
        """)
        conn.commit()
        logger.debug("Trail tables initialized")


# ==================== 免疫报告 (E5) ====================
# 对齐蓝图：05-知识免疫.md + 15-数据字典
# NOTE: immune_reports 表当前无任何业务代码引用，已移除。
# 免疫系统的报告生成通过 Markdown 文件输出（hygieia.py），不写入 SQLite。


# ==================== 压力测试 (E30) ====================
# 对齐蓝图：30-压力测试.md

def init_stress_test_tables():
    """创建压力测试表：stress_test_results"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stress_test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_path TEXT NOT NULL,
                page_title TEXT,
                resilience_score REAL,
                challenges_count INTEGER DEFAULT 0,
                blind_spots_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stress_page ON stress_test_results(page_path)")
        conn.commit()
        logger.debug("Stress test tables initialized")


# ==================== 统一入口 ====================

def init_all_tables():
    """初始化所有缺失的数据库表（幂等，可多次调用）"""
    init_knowledge_immune_tables()
    init_knowledge_graph_tables()
    init_persona_tables()
    init_scorer_tables()
    init_skill_tables()
    init_task_classification_table()
    init_trail_tables()
    init_stress_test_tables()
    logger.info("All database tables initialized")
