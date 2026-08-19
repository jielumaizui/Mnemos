"""SQLite schema initialization for the Ixion flywheel."""

from __future__ import annotations

import sqlite3


class FlywheelSchemaMixin:
    """Initialize Ixion-owned tables without coupling lifecycle logic to DDL."""

    def _init_db(self):
        """初始化数据库"""
        schema = """
        CREATE TABLE IF NOT EXISTS skills (
            skill_name TEXT PRIMARY KEY,
            description TEXT,
            trigger_conditions TEXT,      -- JSON
            input_template TEXT,
            expected_output TEXT,
            source_wiki_pages TEXT,       -- JSON
            usage_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'proposed',
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS skill_usage_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT,
            timestamp TEXT,
            input_data TEXT,
            output_data TEXT,
            status TEXT,
            exception_type TEXT,
            exception_detail TEXT,
            new_scenario BOOLEAN DEFAULT 0,
            user_marked BOOLEAN DEFAULT 0,
            generated_wiki TEXT,
            FOREIGN KEY (skill_name) REFERENCES skills(skill_name)
        );

        CREATE TABLE IF NOT EXISTS wiki_usage_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_path TEXT,
            timestamp TEXT,
            access_type TEXT,             -- read / quote / modify / share
            context TEXT                  -- 使用上下文
        );

        CREATE INDEX IF NOT EXISTS idx_skill_usage ON skill_usage_logs(skill_name, timestamp);
        CREATE INDEX IF NOT EXISTS idx_wiki_usage ON wiki_usage_logs(page_path, timestamp);

        CREATE TABLE IF NOT EXISTS cognitive_decision_assets (
            asset_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            title TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            decision_context TEXT DEFAULT '',
            source_refs TEXT DEFAULT '[]',
            evidence_refs TEXT DEFAULT '[]',
            applicability TEXT DEFAULT '[]',
            failure_modes TEXT DEFAULT '[]',
            verification_recipe TEXT DEFAULT '[]',
            automation_derivative_allowed INTEGER DEFAULT 0,
            status TEXT DEFAULT 'produced',
            confidence REAL DEFAULT 0.0,
            created_at TEXT,
            updated_at TEXT
        );

        -- 画像驱动相关表
        CREATE TABLE IF NOT EXISTS skill_paths (
            path_id TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            stages TEXT,              -- JSON
            cognitive_style TEXT,
            estimated_duration TEXT,
            priority TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS skill_verification_tasks (
            task_id TEXT PRIMARY KEY,
            task_type TEXT,
            description TEXT,
            related_skill TEXT,
            related_blindspot_type TEXT,
            verification_method TEXT,
            expected_outcome TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS persona_flywheel_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_date TEXT,
            persona_version INTEGER,
            gaps_detected INTEGER,
            paths_created INTEGER,
            verifications_created INTEGER,
            flywheel_params TEXT       -- JSON
        );
        """
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.executescript(schema)
            skill_columns = {row[1] for row in conn.execute("PRAGMA table_info(skills)")}
            extra_columns = {
                "version": "INTEGER DEFAULT 1",
                "generation_source": "TEXT DEFAULT ''",
                "last_used": "TEXT DEFAULT ''",
                "created_by": "TEXT DEFAULT ''",
                "parent_version": "INTEGER DEFAULT 0",
                "deviation_log": "TEXT DEFAULT '[]'",
            }
            for column, definition in extra_columns.items():
                if column not in skill_columns:
                    conn.execute(f"ALTER TABLE skills ADD COLUMN {column} {definition}")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skill_versions (
                    skill_name TEXT,
                    version INTEGER,
                    trigger_conditions TEXT,
                    input_template TEXT,
                    expected_output TEXT,
                    change_summary TEXT,
                    created_at TEXT,
                    PRIMARY KEY (skill_name, version)
                )
            """)
            self.behavior_generator._init_task_history(conn)
