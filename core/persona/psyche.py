"""
Signal Store - 用户行为信号数据库

职责：
- 统一管理所有用户行为信号的持久化存储
- 提供信号写入、查询、聚合接口
- 支持信号置信度和外部因素标注

数据库位置：~/.mnemos/user_signals.db
"""
# Psyche — 灵魂女神 — 信号存储，灵魂/行为数据的持久化
# 原模块: signal_store.py



import json
import sqlite3
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime


SIGNAL_DB_PATH = Path.home() / ".mnemos" / "user_signals.db"


# ========== Schema 定义 ==========

SCHEMA_SQL = """
-- 核心信号表：AI对话session
CREATE TABLE IF NOT EXISTS session_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,           -- ISO format
    task_type TEXT,                    -- e.g. "coding/python"
    task_subtype TEXT,

    -- 输入特征
    user_msg_count INTEGER DEFAULT 0,
    avg_user_msg_length REAL DEFAULT 0,
    provided_context_richness REAL DEFAULT 0,  -- 0-1

    -- 交互特征
    correction_count INTEGER DEFAULT 0,
    correction_domains TEXT,           -- JSON list
    follow_up_depth INTEGER DEFAULT 0,

    -- 决策特征
    options_presented INTEGER DEFAULT 0,
    option_selected INTEGER DEFAULT 0,
    selection_rationale TEXT,

    -- 终止特征
    termination_type TEXT,             -- satisfied/abandoned/delegated/progress
    final_feedback TEXT,

    -- 产出特征
    output_type TEXT,                  -- code/document/decision/none
    output_file_count INTEGER DEFAULT 0,
    duration_seconds INTEGER DEFAULT 0,

    -- 画像元数据
    working_dir TEXT,
    agent TEXT DEFAULT 'claude',

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_session_time ON session_signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_session_task ON session_signals(task_type);

-- 核心信号表：知识库交互
CREATE TABLE IF NOT EXISTS knowledge_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_path TEXT NOT NULL,
    action_type TEXT NOT NULL,         -- access/modify/create/reference
    timestamp TEXT NOT NULL,

    -- 深度交互信号
    dwell_time_seconds INTEGER DEFAULT 0,
    scroll_depth REAL DEFAULT 0,       -- 0-1
    copy_count INTEGER DEFAULT 0,
    reference_count INTEGER DEFAULT 0,  -- 被其他页面引用次数

    -- 内容信号
    content_diff TEXT,                 -- 修改内容的diff
    tags_added TEXT,                   -- JSON list
    tags_removed TEXT,                 -- JSON list

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_knowledge_time ON knowledge_signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_knowledge_page ON knowledge_signals(page_path);

-- 核心信号表：微信聊天（只存储自己的发言）
CREATE TABLE IF NOT EXISTS wechat_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    content_hash TEXT NOT NULL,        -- MD5 of content (privacy)
    msg_length INTEGER DEFAULT 0,

    -- 情感与语义
    emotional_valence REAL DEFAULT 0,  -- -1 to 1
    emotional_arousal REAL DEFAULT 0,  -- 0 to 1
    topic_tags TEXT,                   -- JSON list

    -- 上下文
    chat_type TEXT,                    -- private/group
    hour_of_day INTEGER,
    day_of_week INTEGER,               -- 0=Monday
    msg_sequence_in_day INTEGER,       -- 当天第几条消息

    -- 隐私保护
    has_sensitive_content INTEGER DEFAULT 0,  -- 是否含手机号/地址等

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_wechat_time ON wechat_signals(timestamp);

-- 核心信号表：Git行为
CREATE TABLE IF NOT EXISTS git_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_path TEXT NOT NULL,
    commit_hash TEXT,
    timestamp TEXT NOT NULL,

    -- Commit特征
    message_length INTEGER DEFAULT 0,
    has_issue_reference INTEGER DEFAULT 0,
    has_pr_reference INTEGER DEFAULT 0,

    -- 代码变更
    files_changed INTEGER DEFAULT 0,
    lines_added INTEGER DEFAULT 0,
    lines_deleted INTEGER DEFAULT 0,
    test_files_changed INTEGER DEFAULT 0,

    -- 推断特征
    commit_type TEXT,                  -- feat/fix/docs/refactor/test/chore
    is_weekend INTEGER DEFAULT 0,
    hour_of_day INTEGER,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_git_time ON git_signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_git_repo ON git_signals(repo_path);

-- 核心信号表：文件系统行为
CREATE TABLE IF NOT EXISTS file_system_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT,
    action_type TEXT NOT NULL,         -- create/modify/delete/move
    timestamp TEXT NOT NULL,

    -- 文件特征
    file_extension TEXT,
    directory_depth INTEGER DEFAULT 0,
    project_name TEXT,

    -- 组织特征
    is_in_inbox INTEGER DEFAULT 0,     -- 是否在临时/下载目录
    is_versioned INTEGER DEFAULT 0,    -- 是否在git仓库中

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_fs_time ON file_system_signals(timestamp);

-- 信号元数据：置信度与外部因素
CREATE TABLE IF NOT EXISTS signal_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_table TEXT NOT NULL,        -- session/knowledge/wechat/git/fs
    signal_id INTEGER NOT NULL,

    -- 质量标注
    confidence REAL DEFAULT 1.0,       -- 0-1
    possible_external_factors TEXT,    -- JSON list, e.g. ["company_policy"]

    -- 处理状态
    processed INTEGER DEFAULT 0,       -- 是否已纳入画像分析
    processed_at TEXT,

    -- 上下文
    session_context TEXT,              -- JSON

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_meta_processed ON signal_metadata(processed);
CREATE INDEX IF NOT EXISTS idx_meta_table_id ON signal_metadata(signal_table, signal_id);

-- 信号聚合索引（加速画像分析）
CREATE TABLE IF NOT EXISTS signal_daily_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,                -- YYYY-MM-DD
    source_type TEXT NOT NULL,         -- session/knowledge/wechat/git/fs
    signal_count INTEGER DEFAULT 0,
    summary_json TEXT,                 -- 聚合摘要

    UNIQUE(date, source_type)
);

CREATE INDEX IF NOT EXISTS idx_daily_date ON signal_daily_index(date);

-- 画像基线版本记录
CREATE TABLE IF NOT EXISTS persona_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    generated_at TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,

    -- 三层雷达（JSON存储完整画像）
    energy_profile TEXT,               -- JSON
    cognitive_profile TEXT,            -- JSON
    value_profile TEXT,                -- JSON

    -- 盲区画像
    blindspot_profile TEXT,            -- JSON

    -- 元数据
    signal_count_used INTEGER,
    user_confirmed INTEGER DEFAULT 0,  -- 用户是否确认
    confirmed_at TEXT,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_persona_version ON persona_versions(version);

-- 核心信号表：Memos笔记
CREATE TABLE IF NOT EXISTS memos_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memo_uid TEXT,                     -- memos UID
    timestamp TEXT NOT NULL,           -- 笔记创建时间

    -- 内容特征
    content_length INTEGER DEFAULT 0,
    has_title INTEGER DEFAULT 0,       -- 是否有markdown标题
    has_list INTEGER DEFAULT 0,        -- 是否有列表
    has_code_block INTEGER DEFAULT 0,  -- 是否有代码块
    has_link INTEGER DEFAULT 0,        -- 是否有链接
    image_count INTEGER DEFAULT 0,     -- 图片数量

    -- 标签特征
    tag_count INTEGER DEFAULT 0,
    tags_json TEXT,                    -- JSON list of tags

    -- 行为特征
    is_ai_generated INTEGER DEFAULT 0, -- 是否AI生成
    ai_agent TEXT,                     -- 哪个AI生成

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_memos_time ON memos_signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_memos_ai ON memos_signals(is_ai_generated);
"""


# ========== 数据类 ==========

@dataclass
class SessionSignal:
    """AI对话session信号"""
    session_id: str
    timestamp: str
    task_type: str = ""
    task_subtype: str = ""
    user_msg_count: int = 0
    avg_user_msg_length: float = 0
    provided_context_richness: float = 0
    correction_count: int = 0
    correction_domains: List[str] = None
    follow_up_depth: int = 0
    options_presented: int = 0
    option_selected: int = 0
    selection_rationale: str = ""
    termination_type: str = ""
    final_feedback: str = ""
    output_type: str = ""
    output_file_count: int = 0
    duration_seconds: int = 0
    working_dir: str = ""
    agent: str = "claude"


@dataclass
class WechatSignal:
    """微信聊天信号（仅自己的发言）"""
    timestamp: str
    content_hash: str
    msg_length: int = 0
    emotional_valence: float = 0
    emotional_arousal: float = 0
    topic_tags: List[str] = None
    chat_type: str = ""
    hour_of_day: int = 0
    day_of_week: int = 0
    msg_sequence_in_day: int = 0
    has_sensitive_content: bool = False


@dataclass
class GitSignal:
    """Git行为信号"""
    repo_path: str
    commit_hash: str
    timestamp: str
    message_length: int = 0
    has_issue_reference: bool = False
    has_pr_reference: bool = False
    files_changed: int = 0
    lines_added: int = 0
    lines_deleted: int = 0
    test_files_changed: int = 0
    commit_type: str = ""
    is_weekend: bool = False
    hour_of_day: int = 0


@dataclass
class MemosSignal:
    """Memos笔记信号"""
    timestamp: str
    content_length: int = 0
    has_title: bool = False
    has_list: bool = False
    has_code_block: bool = False
    has_link: bool = False
    image_count: int = 0
    tag_count: int = 0
    tags_json: str = ""
    is_ai_generated: bool = False
    ai_agent: str = ""
    memo_uid: str = ""


# ========== SignalStore 类 ==========

class SignalStore:
    """信号存储管理器"""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or SIGNAL_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()

    # ---- Session Signals ----

    def insert_session_signal(self, signal: SessionSignal, session_context: dict = None) -> int:
        """插入session信号，返回id"""
        data = asdict(signal)
        # JSON序列化列表
        if data.get("correction_domains"):
            data["correction_domains"] = json.dumps(data["correction_domains"], ensure_ascii=False)

        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute("""
                INSERT INTO session_signals (
                    session_id, timestamp, task_type, task_subtype,
                    user_msg_count, avg_user_msg_length, provided_context_richness,
                    correction_count, correction_domains, follow_up_depth,
                    options_presented, option_selected, selection_rationale,
                    termination_type, final_feedback, output_type, output_file_count,
                    duration_seconds, working_dir, agent
                ) VALUES (
                    :session_id, :timestamp, :task_type, :task_subtype,
                    :user_msg_count, :avg_user_msg_length, :provided_context_richness,
                    :correction_count, :correction_domains, :follow_up_depth,
                    :options_presented, :option_selected, :selection_rationale,
                    :termination_type, :final_feedback, :output_type, :output_file_count,
                    :duration_seconds, :working_dir, :agent
                )
            """, data)
            signal_id = cursor.lastrowid

            # 插入元数据（支持 session_context JSON）
            context_json = json.dumps(session_context, ensure_ascii=False) if session_context else None
            conn.execute("""
                INSERT INTO signal_metadata (signal_table, signal_id, confidence, processed, session_context)
                VALUES (?, ?, ?, ?, ?)
            """, ("session", signal_id, 1.0, 0, context_json))
            conn.commit()
            return signal_id

    def get_recent_session_signals(self, days: int = 90) -> List[Dict]:
        """获取最近N天的session信号"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM session_signals
                WHERE timestamp >= date('now', ?)
                ORDER BY timestamp DESC
            """, (f'-{days} days',))
            return [dict(row) for row in cursor.fetchall()]

    # ---- Wechat Signals ----

    def insert_wechat_signal(self, signal: WechatSignal) -> int:
        """插入微信信号"""
        data = asdict(signal)
        if data.get("topic_tags"):
            data["topic_tags"] = json.dumps(data["topic_tags"], ensure_ascii=False)

        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute("""
                INSERT INTO wechat_signals (
                    timestamp, content_hash, msg_length,
                    emotional_valence, emotional_arousal, topic_tags,
                    chat_type, hour_of_day, day_of_week, msg_sequence_in_day,
                    has_sensitive_content
                ) VALUES (
                    :timestamp, :content_hash, :msg_length,
                    :emotional_valence, :emotional_arousal, :topic_tags,
                    :chat_type, :hour_of_day, :day_of_week, :msg_sequence_in_day,
                    :has_sensitive_content
                )
            """, data)
            signal_id = cursor.lastrowid

            conn.execute("""
                INSERT INTO signal_metadata (signal_table, signal_id, confidence, processed)
                VALUES (?, ?, ?, ?)
            """, ("wechat", signal_id, 0.8, 0))  # 微信信号置信度稍低
            conn.commit()
            return signal_id

    def get_recent_wechat_signals(self, days: int = 90) -> List[Dict]:
        """获取最近N天的微信信号"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM wechat_signals
                WHERE timestamp >= date('now', ?)
                ORDER BY timestamp DESC
            """, (f'-{days} days',))
            return [dict(row) for row in cursor.fetchall()]

    # ---- Memos Signals ----

    def insert_memos_signal(self, signal: MemosSignal) -> int:
        """插入memos笔记信号"""
        data = asdict(signal)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute("""
                INSERT INTO memos_signals (
                    memo_uid, timestamp, content_length,
                    has_title, has_list, has_code_block, has_link, image_count,
                    tag_count, tags_json, is_ai_generated, ai_agent
                ) VALUES (
                    :memo_uid, :timestamp, :content_length,
                    :has_title, :has_list, :has_code_block, :has_link, :image_count,
                    :tag_count, :tags_json, :is_ai_generated, :ai_agent
                )
            """, data)
            signal_id = cursor.lastrowid

            conn.execute("""
                INSERT INTO signal_metadata (signal_table, signal_id, confidence, processed)
                VALUES (?, ?, ?, ?)
            """, ("memos", signal_id, 0.8 if not signal.is_ai_generated else 0.5, 0))
            conn.commit()
            return signal_id

    def get_recent_memos_signals(self, days: int = 90) -> List[Dict]:
        """获取最近N天的memos信号"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM memos_signals
                WHERE timestamp >= date('now', ?)
                ORDER BY timestamp DESC
            """, (f'-{days} days',))
            return [dict(row) for row in cursor.fetchall()]

    # ---- Git Signals ----

    def insert_git_signal(self, signal: GitSignal) -> int:
        """插入git信号"""
        data = asdict(signal)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute("""
                INSERT INTO git_signals (
                    repo_path, commit_hash, timestamp,
                    message_length, has_issue_reference, has_pr_reference,
                    files_changed, lines_added, lines_deleted, test_files_changed,
                    commit_type, is_weekend, hour_of_day
                ) VALUES (
                    :repo_path, :commit_hash, :timestamp,
                    :message_length, :has_issue_reference, :has_pr_reference,
                    :files_changed, :lines_added, :lines_deleted, :test_files_changed,
                    :commit_type, :is_weekend, :hour_of_day
                )
            """, data)
            signal_id = cursor.lastrowid

            # Git信号可能有外部因素（公司规范）
            confidence = 0.7  # 默认较低，需要外部因素标注
            conn.execute("""
                INSERT INTO signal_metadata (signal_table, signal_id, confidence, processed, possible_external_factors)
                VALUES (?, ?, ?, ?, ?)
            """, ("git", signal_id, confidence, 0, json.dumps(["possible_company_policy"])))
            conn.commit()
            return signal_id

    # ---- 通用查询 ----

    ALLOWED_SOURCES = {"session", "git", "memos", "wiki", "file_system", "wechat"}

    def _validate_source(self, source_type: str):
        """校验数据源类型，防止 SQL 注入"""
        if source_type not in self.ALLOWED_SOURCES:
            raise ValueError(f"非法数据源: {source_type}")

    def get_unprocessed_signals(self, source_type: str, limit: int = 1000) -> List[Dict]:
        """获取未处理的信号（用于画像分析）"""
        self._validate_source(source_type)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT s.*, m.confidence, m.possible_external_factors
                FROM {} s
                JOIN signal_metadata m ON m.signal_id = s.id AND m.signal_table = ?
                WHERE m.processed = 0
                ORDER BY s.timestamp ASC
                LIMIT ?
            """.format(f"{source_type}_signals"), (source_type, limit))
            return [dict(row) for row in cursor.fetchall()]

    def mark_signals_processed(self, source_type: str, signal_ids: List[int]):
        """标记信号已处理"""
        if not signal_ids:
            return
        placeholders = ",".join("?" * len(signal_ids))
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute(f"""
                UPDATE signal_metadata
                SET processed = 1, processed_at = ?
                WHERE signal_table = ? AND signal_id IN ({placeholders})
            """, (datetime.now().isoformat(), source_type, *signal_ids))
            conn.commit()

    def insert_knowledge_signal(self, page_path: str, action_type: str, timestamp: str,
                                 tags_added: str = "[]", tags_removed: str = "[]") -> int:
        """插入知识库交互信号"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute("""
                INSERT INTO knowledge_signals (
                    page_path, action_type, timestamp,
                    tags_added, tags_removed
                ) VALUES (?, ?, ?, ?, ?)
            """, (page_path, action_type, timestamp, tags_added, tags_removed))
            signal_id = cursor.lastrowid
            conn.execute("""
                INSERT INTO signal_metadata (signal_table, signal_id, confidence, processed)
                VALUES (?, ?, ?, ?)
            """, ("knowledge", signal_id, 0.7, 0))
            conn.commit()
            return signal_id

    def insert_file_system_signal(self, file_path: str, action_type: str, timestamp: str,
                                   file_extension: str = "", directory_depth: int = 0,
                                   project_name: str = "", is_in_inbox: int = 0,
                                   is_versioned: int = 0) -> int:
        """插入文件系统行为信号"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute("""
                INSERT INTO file_system_signals (
                    file_path, action_type, timestamp,
                    file_extension, directory_depth, project_name,
                    is_in_inbox, is_versioned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (file_path, action_type, timestamp, file_extension,
                  directory_depth, project_name, is_in_inbox, is_versioned))
            signal_id = cursor.lastrowid
            conn.execute("""
                INSERT INTO signal_metadata (signal_table, signal_id, confidence, processed)
                VALUES (?, ?, ?, ?)
            """, ("file_system", signal_id, 0.6, 0))
            conn.commit()
            return signal_id

    def get_signal_stats(self, days: int = 30) -> Dict[str, Any]:
        """获取信号统计摘要"""
        stats = {}
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            for source in ["session", "knowledge", "wechat", "git", "file_system", "memos"]:
                cursor = conn.execute(f"""
                    SELECT COUNT(*) FROM {source}_signals
                    WHERE timestamp >= date('now', ?)
                """, (f'-{days} days',))
                stats[source] = cursor.fetchone()[0]
        return stats

    def get_daily_summary(self, date: str) -> Dict[str, Any]:
        """获取某天的信号聚合摘要"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM signal_daily_index WHERE date = ?
            """, (date,))
            rows = cursor.fetchall()
            return {row["source_type"]: dict(row) for row in rows}

    # ---- 跨项目隔离 ----

    def get_recent_session_signals_by_project(self, working_dir: str, days: int = 90) -> List[Dict]:
        """
        按工作目录（项目）获取session信号。

        用于防止跨项目污染：不同项目的信号可能反映不同的工作偏好，
        不应混为一谈。
        """
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM session_signals
                WHERE working_dir LIKE ?
                  AND timestamp >= date('now', ?)
                ORDER BY timestamp DESC
            """, (f"%{working_dir}%", f'-{days} days'))
            return [dict(row) for row in cursor.fetchall()]

    def get_signal_projects(self, days: int = 90) -> List[Dict]:
        """获取所有有信号的项目列表"""
        projects = []
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            # Session项目
            cursor = conn.execute("""
                SELECT working_dir, COUNT(*) as count
                FROM session_signals
                WHERE timestamp >= date('now', ?)
                  AND working_dir IS NOT NULL
                GROUP BY working_dir
                ORDER BY count DESC
            """, (f'-{days} days',))
            for row in cursor.fetchall():
                projects.append({
                    "type": "session",
                    "identifier": row["working_dir"],
                    "signal_count": row["count"],
                })
            # Git项目
            cursor = conn.execute("""
                SELECT repo_path, COUNT(*) as count
                FROM git_signals
                WHERE timestamp >= date('now', ?)
                GROUP BY repo_path
                ORDER BY count DESC
            """, (f'-{days} days',))
            for row in cursor.fetchall():
                projects.append({
                    "type": "git",
                    "identifier": row["repo_path"],
                    "signal_count": row["count"],
                })
        return projects

    # ---- 去重检查 ----

    def session_exists(self, session_id: str) -> bool:
        """检查 session 信号是否已存在"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM session_signals WHERE session_id = ? LIMIT 1",
                (session_id,)
            )
            return cursor.fetchone() is not None

    def git_commit_exists(self, commit_hash: str) -> bool:
        """检查 git commit 信号是否已存在"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM git_signals WHERE commit_hash = ? LIMIT 1",
                (commit_hash,)
            )
            return cursor.fetchone() is not None

    def memos_exists(self, memo_uid: str) -> bool:
        """检查 memos 信号是否已存在（memo_uid 为空时不检查）"""
        if not memo_uid:
            return False
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM memos_signals WHERE memo_uid = ? LIMIT 1",
                (memo_uid,)
            )
            return cursor.fetchone() is not None

    def knowledge_page_exists(self, page_path: str, since: str = None) -> bool:
        """检查知识库页面信号是否已存在"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            if since:
                cursor = conn.execute(
                    "SELECT 1 FROM knowledge_signals WHERE page_path = ? AND timestamp >= ? LIMIT 1",
                    (page_path, since)
                )
            else:
                cursor = conn.execute(
                    "SELECT 1 FROM knowledge_signals WHERE page_path = ? LIMIT 1",
                    (page_path,)
                )
            return cursor.fetchone() is not None

    def file_system_exists(self, file_path: str, since: str = None) -> bool:
        """检查文件系统信号是否已存在"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            if since:
                cursor = conn.execute(
                    "SELECT 1 FROM file_system_signals WHERE file_path = ? AND timestamp >= ? LIMIT 1",
                    (file_path, since)
                )
            else:
                cursor = conn.execute(
                    "SELECT 1 FROM file_system_signals WHERE file_path = ? LIMIT 1",
                    (file_path,)
                )
            return cursor.fetchone() is not None

    def wechat_exists(self, content_hash: str) -> bool:
        """检查微信信号是否已存在"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM wechat_signals WHERE content_hash = ? LIMIT 1",
                (content_hash,)
            )
            return cursor.fetchone() is not None

    def get_project_isolated_signals(self, project_dir: str, days: int = 90) -> Dict[str, List[Dict]]:
        """
        获取单个项目隔离后的所有信号。

        Returns:
            {"session": [...], "git": [...], "file_system": [...]}
        """
        results = {
            "session": self.get_recent_session_signals_by_project(project_dir, days),
        }

        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row

            # Git信号（匹配repo_path前缀）
            cursor = conn.execute("""
                SELECT * FROM git_signals
                WHERE repo_path LIKE ?
                  AND timestamp >= date('now', ?)
                ORDER BY timestamp DESC
            """, (f"%{project_dir}%", f'-{days} days'))
            results["git"] = [dict(row) for row in cursor.fetchall()]

            # 文件系统信号
            cursor = conn.execute("""
                SELECT * FROM file_system_signals
                WHERE file_path LIKE ?
                  AND timestamp >= date('now', ?)
                ORDER BY timestamp DESC
            """, (f"%{project_dir}%", f'-{days} days'))
            results["file_system"] = [dict(row) for row in cursor.fetchall()]

        return results

    # ---- Persona 版本管理 ----

    def save_persona_version(self, version: int, period_start: str, period_end: str,
                             energy: Dict, cognitive: Dict, value: Dict,
                             blindspot: Dict, signal_count: int) -> int:
        """保存画像版本"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute("""
                INSERT INTO persona_versions (
                    version, generated_at, period_start, period_end,
                    energy_profile, cognitive_profile, value_profile,
                    blindspot_profile, signal_count_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                version, datetime.now().isoformat(), period_start, period_end,
                json.dumps(energy, ensure_ascii=False),
                json.dumps(cognitive, ensure_ascii=False),
                json.dumps(value, ensure_ascii=False),
                json.dumps(blindspot, ensure_ascii=False),
                signal_count
            ))
            conn.commit()
            return cursor.lastrowid

    def get_latest_persona_version(self) -> Optional[Dict]:
        """获取最新画像版本"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM persona_versions
                ORDER BY version DESC LIMIT 1
            """)
            row = cursor.fetchone()
            if row:
                result = dict(row)
                result["energy_profile"] = json.loads(result["energy_profile"] or "{}")
                result["cognitive_profile"] = json.loads(result["cognitive_profile"] or "{}")
                result["value_profile"] = json.loads(result["value_profile"] or "{}")
                result["blindspot_profile"] = json.loads(result["blindspot_profile"] or "{}")
                return result
            return None

    def update_blindspot_profile(self, blindspot_data: Dict) -> bool:
        """更新最新画像版本的盲区数据（独立保存时调用）"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute("""
                SELECT id FROM persona_versions ORDER BY version DESC LIMIT 1
            """)
            row = cursor.fetchone()
            if not row:
                return False
            latest_id = row[0]
            conn.execute("""
                UPDATE persona_versions
                SET blindspot_profile = ?
                WHERE id = ?
            """, (json.dumps(blindspot_data, ensure_ascii=False), latest_id))
            conn.commit()
            return True


# ========== 单例 ==========

_signal_store: Optional[SignalStore] = None


def get_signal_store() -> SignalStore:
    """获取SignalStore单例"""
    global _signal_store
    if _signal_store is None:
        _signal_store = SignalStore()
    return _signal_store


# ========== 便捷函数 ==========

def log_session_signal(**kwargs) -> int:
    """便捷函数：记录session信号"""
    signal = SessionSignal(**kwargs)
    return get_signal_store().insert_session_signal(signal)


def get_recent_signals_summary(days: int = 7) -> str:
    """获取最近信号摘要（用于调试）"""
    store = get_signal_store()
    stats = store.get_signal_stats(days=days)
    lines = [f"📊 最近{days}天信号统计:"]
    for source, count in stats.items():
        lines.append(f"  {source}: {count}")
    total = sum(stats.values())
    lines.append(f"  总计: {total}")
    return "\n".join(lines)


if __name__ == "__main__":
    # 测试
    store = SignalStore()
    print("✅ SignalStore initialized")
    print(f"   Database: {store.db_path}")
    print(get_recent_signals_summary(days=7))
