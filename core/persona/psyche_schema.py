"""Canonical SQLite schema text for the Persona signal store."""

from core.persona.cognitive_profile import PROFILE_SCHEMA_SQL

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

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, timestamp, agent)
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

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(page_path, timestamp, action_type)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_time ON knowledge_signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_knowledge_page ON knowledge_signals(page_path);

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

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(commit_hash)
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

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(file_path, timestamp, action_type)
);

CREATE INDEX IF NOT EXISTS idx_fs_time ON file_system_signals(timestamp);

-- 信号元数据：置信度与外部因素
CREATE TABLE IF NOT EXISTS signal_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_table TEXT NOT NULL,        -- session/knowledge/git/fs
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
    source_type TEXT NOT NULL,         -- session/knowledge/git/fs
    signal_count INTEGER DEFAULT 0,
    summary_json TEXT,                 -- 聚合摘要

    UNIQUE(date, source_type)
);

CREATE INDEX IF NOT EXISTS idx_daily_date ON signal_daily_index(date);

-- 画像行为提示使用追踪
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
CREATE INDEX IF NOT EXISTS idx_behavior_prompt_time ON behavior_prompt_signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_behavior_prompt_agent ON behavior_prompt_signals(agent);
CREATE INDEX IF NOT EXISTS idx_behavior_prompt_source ON behavior_prompt_signals(source);

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
    calibration_score REAL,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_persona_version ON persona_versions(version);

-- Canonical Persona semantic revisions.  ``persona_versions`` above is a
-- legacy migration source only: runtime writers must use this immutable
-- ledger and its single global head below.
CREATE TABLE IF NOT EXISTS persona_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL UNIQUE,
    content_hash TEXT NOT NULL UNIQUE,
    supersedes_revision_id TEXT REFERENCES persona_revisions(revision_id),
    source_cursor TEXT NOT NULL,
    materiality_evidence TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,
    energy_profile TEXT NOT NULL,
    cognitive_profile TEXT NOT NULL,
    value_profile TEXT NOT NULL,
    blindspot_profile TEXT NOT NULL,
    signal_count_used INTEGER NOT NULL,
    user_confirmed INTEGER NOT NULL DEFAULT 0,
    confirmed_at TEXT,
    calibration_score REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(supersedes_revision_id) REFERENCES persona_revisions(revision_id)
);

CREATE INDEX IF NOT EXISTS idx_persona_revisions_supersedes
ON persona_revisions(supersedes_revision_id);

CREATE TABLE IF NOT EXISTS persona_revision_heads (
    scope_key TEXT PRIMARY KEY CHECK(scope_key = 'global'),
    revision_id TEXT NOT NULL UNIQUE REFERENCES persona_revisions(revision_id),
    updated_at TEXT NOT NULL
);

-- Separate immutable telemetry/event streams.  The corresponding Persona
-- revision is the semantic state transition; these rows make calibration and
-- blindspot correction/revocation replayable without mutating that history.
CREATE TABLE IF NOT EXISTS persona_blindspot_events (
    event_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL UNIQUE REFERENCES persona_revisions(revision_id),
    supersedes_event_id TEXT REFERENCES persona_blindspot_events(event_id),
    event_type TEXT NOT NULL CHECK(event_type IN ('applied', 'revoked')),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_persona_blindspot_events_supersedes
ON persona_blindspot_events(supersedes_event_id);

CREATE TABLE IF NOT EXISTS persona_calibration_events (
    event_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL UNIQUE REFERENCES persona_revisions(revision_id),
    supersedes_event_id TEXT REFERENCES persona_calibration_events(event_id),
    event_type TEXT NOT NULL CHECK(event_type IN ('applied', 'revoked')),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_persona_calibration_events_supersedes
ON persona_calibration_events(supersedes_event_id);

CREATE TRIGGER IF NOT EXISTS persona_revisions_immutable_update
BEFORE UPDATE ON persona_revisions
BEGIN
    SELECT RAISE(ABORT, 'persona_revisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS persona_revisions_immutable_delete
BEFORE DELETE ON persona_revisions
BEGIN
    SELECT RAISE(ABORT, 'persona_revisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS persona_blindspot_events_immutable_update
BEFORE UPDATE ON persona_blindspot_events
BEGIN
    SELECT RAISE(ABORT, 'persona_blindspot_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS persona_blindspot_events_immutable_delete
BEFORE DELETE ON persona_blindspot_events
BEGIN
    SELECT RAISE(ABORT, 'persona_blindspot_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS persona_calibration_events_immutable_update
BEFORE UPDATE ON persona_calibration_events
BEGIN
    SELECT RAISE(ABORT, 'persona_calibration_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS persona_calibration_events_immutable_delete
BEFORE DELETE ON persona_calibration_events
BEGIN
    SELECT RAISE(ABORT, 'persona_calibration_events are append-only');
END;

-- 核心信号表：笔记
CREATE TABLE IF NOT EXISTS note_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_uid TEXT,                     -- 笔记 UID
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

CREATE INDEX IF NOT EXISTS idx_note_time ON note_signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_note_ai ON note_signals(is_ai_generated);

-- 核心信号表：外部文档导入
CREATE TABLE IF NOT EXISTS document_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,                   -- doc-{hash}
    filename TEXT,
    doc_type TEXT,                     -- pdf/ppt/xlsx/docx/epub/...
    doc_category TEXT,                 -- book/strategy/data/report/manual/reference
    title TEXT,
    key_topics TEXT,                   -- JSON array
    entity_type TEXT,                  -- concept/project/dataset/retrospective/technology
    page_count INTEGER DEFAULT 0,
    import_timestamp TEXT,
    import_source TEXT,                -- file_path
    confidence REAL DEFAULT 0.0,
    processed INTEGER DEFAULT 0,       -- 是否已参与画像分析
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_doc_time ON document_signals(import_timestamp);
CREATE INDEX IF NOT EXISTS idx_doc_category ON document_signals(doc_category);
CREATE INDEX IF NOT EXISTS idx_doc_processed ON document_signals(processed);

-- 核心信号表：微信聊天
CREATE TABLE IF NOT EXISTS wechat_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    content_hash TEXT,
    msg_length INTEGER DEFAULT 0,
    has_sensitive_content INTEGER DEFAULT 0,
    emotional_valence REAL DEFAULT 0.0,
    emotional_arousal REAL DEFAULT 0.0,
    topic_tags TEXT,
    chat_type TEXT DEFAULT 'unknown',
    hour_of_day INTEGER DEFAULT 0,
    day_of_week INTEGER DEFAULT 0,
    msg_sequence_in_day INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(timestamp, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_wechat_time ON wechat_signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_wechat_hash ON wechat_signals(content_hash);

-- Layer 5 反射信号（Insight / Feedback / CognitiveShift 下游）
CREATE TABLE IF NOT EXISTS reflection_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dimension TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL DEFAULT 0.0,
    source TEXT NOT NULL,
    source_event_id TEXT DEFAULT '',
    timestamp TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reflection_signal_dim ON reflection_signals(dimension);
CREATE INDEX IF NOT EXISTS idx_reflection_signal_source ON reflection_signals(source);

-- Append-only correction ledger. The source row remains available for audit,
-- while active readers exclude every signal named here.
CREATE TABLE IF NOT EXISTS reflection_signal_suppressions (
    suppression_id TEXT PRIMARY KEY,
    signal_id INTEGER NOT NULL,
    source_event_id TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    before_hash TEXT NOT NULL,
    after_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(signal_id) REFERENCES reflection_signals(id)
);

CREATE INDEX IF NOT EXISTS idx_reflection_signal_suppressions_signal
ON reflection_signal_suppressions(signal_id);

CREATE TRIGGER IF NOT EXISTS reflection_signal_suppressions_no_update
BEFORE UPDATE ON reflection_signal_suppressions
BEGIN
    SELECT RAISE(ABORT, 'reflection signal suppressions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS reflection_signal_suppressions_no_delete
BEFORE DELETE ON reflection_signal_suppressions
BEGIN
    SELECT RAISE(ABORT, 'reflection signal suppressions are append-only');
END;
""" + PROFILE_SCHEMA_SQL
