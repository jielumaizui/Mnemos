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
from functools import wraps
from os import PathLike
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import asdict
from datetime import datetime
from unittest.mock import Mock

from core.db_utils import sqlite_artifact_exists, validate_sql_identifier
from core.cognitive.material_effect_schema import (
    initialize_material_effect_schema,
)
from core.cognitive.state_contract import sha256_json
from core.persona.psyche_models import GitSignal, NoteSignal, SessionSignal, WechatSignal
from core.persona.cognitive_profile import (  # noqa: F401
    CognitiveProfileRepository,
    ProfileAssertion,
    ProfileSignal,
    ProfileUsageLog,
    register_cognitive_profile_runtime_schema,
    validate_cognitive_profile_runtime_schema,
)
from core.persona.reflection_signal_persistence import (
    ensure_source_event_schema,
    persist_reflection_signal,
)
from core.persona.psyche_constants import (  # noqa: F401
    CORE,
    DURATION_BUCKET_WEEK_DAYS,
    SIGNAL_STORE_BUSY_TIMEOUT_MS,
    SIGNAL_STORE_DURATION_BUCKET_QUARTER_DAYS,
    SIGNAL_STORE_GET_PROJECT_ISOLATED_SIGNALS_PROJECT_DIR_DAYS,
    SIGNAL_STORE_GET_SIGNAL_HEALTH_DAYS,
    SIGNAL_STORE_GET_SIGNAL_PROJECTS_DAYS,
    SIGNAL_STORE_GET_SIGNAL_STATS_DAYS,
    SIGNAL_STORE_SQLITE_TIMEOUT_SECONDS,
)
from core.persona.psyche_material_contracts import (  # noqa: F401
    PERSONA_BLINDSPOT_ACTION,
    PERSONA_BLINDSPOT_REVOKE_ACTION,
    PERSONA_CALIBRATION_ACTION,
    PERSONA_CALIBRATION_REVOKE_ACTION,
    PERSONA_DECISION_CONTRACT_ID,
    PERSONA_DECISION_CONTRACT_REVISION,
    PERSONA_DECISION_CONTRACT_TEXT,
    PERSONA_DECISION_PRODUCER_HASH,
    PERSONA_VERSION_ACTION,
    PERSONA_VERSION_EXECUTOR,
    PERSONA_VERSION_OWNER,
    PersonaBlindspotEffectOracle,
    PersonaBlindspotRevokeEffectOracle,
    PersonaCalibrationEffectOracle,
    PersonaCalibrationRevokeEffectOracle,
    PersonaVersionEffectOracle,
    authorize_exact_persona_material_action,
    persona_version_material_action_binding,
)
from core.persona.psyche_persona import SignalPersonaMixin
from core.persona.psyche_schema import SCHEMA_SQL
from core.persona.profile_assertion_schema import register_profile_assertion_schema
from core.utils import LazyPath

import logging

logger = logging.getLogger(__name__)
SIGNAL_DB_PATH = LazyPath("database_dir", "user_signals.db")


# ========== Schema 定义 ==========


# ========== SignalStore 类 ==========


class SignalStore(SignalPersonaMixin):
    """信号存储管理器"""

    def __getattribute__(self, name: str):
        attr = object.__getattribute__(self, name)
        if name.startswith("_") or name == "close" or not callable(attr):
            return attr
        class_attr = getattr(type(self), name, None)
        if not callable(class_attr):
            return attr
        try:
            pool = object.__getattribute__(self, "_pool")
        except AttributeError:
            return attr
        if getattr(pool, "_persistent", True):
            return attr

        @wraps(attr)
        def release_after_call(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            finally:
                pool.release_transient_connections()

        return release_after_call

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        config: Any | None = None,
        sqlite_timeout: float = SIGNAL_STORE_SQLITE_TIMEOUT_SECONDS,
        busy_timeout_ms: int = SIGNAL_STORE_BUSY_TIMEOUT_MS,
        initialize_schema: bool = False,
    ):
        from core.db_utils import SqlitePool
        from core.config import get_config

        self.db_path = Path(db_path) if db_path is not None else Path(SIGNAL_DB_PATH)
        self._ownership_config = config if config is not None else get_config()
        if initialize_schema:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        elif not sqlite_artifact_exists(self.db_path):
            raise RuntimeError(
                "SignalStore is uninitialized; use an explicit bootstrap or reconciliation command"
            )
        self._pool = SqlitePool(
            self.db_path,
            timeout=sqlite_timeout,
            busy_timeout_ms=busy_timeout_ms,
            persistent=False,
        )
        self._cognitive_profiles = CognitiveProfileRepository(
            self._pool,
            ownership_config=self._ownership_config,
        )
        if initialize_schema:
            self._init_db()
        else:
            try:
                validate_cognitive_profile_runtime_schema(self._pool.get_conn())
            except (RuntimeError, sqlite3.Error) as exc:
                self._pool.close()
                raise RuntimeError("SignalStore schema requires explicit reconciliation") from exc
        release_transient = getattr(self._pool, "release_transient_connections", None)
        if callable(release_transient):
            release_transient()

    def close(self):
        """关闭持久连接"""
        if hasattr(self, "_pool"):
            self._pool.close()

    def _init_db(self):
        """初始化数据库"""
        conn = self._pool.get_conn()
        initialize_material_effect_schema(conn)
        conn.executescript(SCHEMA_SQL)
        register_profile_assertion_schema(conn)
        register_cognitive_profile_runtime_schema(conn)
        persona_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(persona_versions)").fetchall()
        }
        if "calibration_score" not in persona_columns:
            conn.execute("ALTER TABLE persona_versions ADD COLUMN calibration_score REAL")
        ensure_source_event_schema(conn)
        conn.commit()

    # ---- Session Signals ----

    def insert_session_signal(
        self, signal: SessionSignal, session_context: dict | None = None
    ) -> int:
        """插入session信号，返回id。信号表与metadata表在同一事务中写入。"""
        data = asdict(signal)
        data["avg_user_msg_length"] = float(signal.avg_user_msg_length or 0)
        # JSON序列化列表
        if data.get("correction_domains"):
            data["correction_domains"] = json.dumps(data["correction_domains"], ensure_ascii=False)

        conn = self._pool.get_conn()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO session_signals (
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
        """,
            data,
        )
        signal_id = cursor.lastrowid
        if cursor.rowcount == 0 or signal_id is None:
            # 被 IGNORE 了（重复信号），查询已有记录的 id
            existing = conn.execute(
                "SELECT id FROM session_signals WHERE session_id = ? AND timestamp = ? AND agent = ?",  # noqa: E501
                (data["session_id"], data["timestamp"], data["agent"]),
            ).fetchone()
            return existing[0] if existing else 0

        # 插入元数据（支持 session_context JSON）
        context_json = json.dumps(session_context, ensure_ascii=False) if session_context else None
        conn.execute(
            """
            INSERT INTO signal_metadata (
                signal_table, signal_id, confidence, processed, session_context
            )
            VALUES (?, ?, ?, ?, ?)
        """,
            ("session", signal_id, 1.0, 0, context_json),
        )
        conn.commit()
        return signal_id

    def get_recent_session_signals(
        self, days: int = SIGNAL_STORE_DURATION_BUCKET_QUARTER_DAYS
    ) -> List[Dict]:
        """获取最近N天的session信号"""
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        cursor = conn.execute(
            """
            SELECT * FROM session_signals
            WHERE timestamp >= date('now', ?)
            ORDER BY timestamp DESC
        """,
            (f"-{days} days",),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_recent_reflection_signals(
        self, days: int = SIGNAL_STORE_DURATION_BUCKET_QUARTER_DAYS
    ) -> List[Dict]:
        """获取最近N天的 Layer 5 反射信号（供画像分析消费）。"""
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        cursor = conn.execute(
            """
            SELECT * FROM reflection_signals
            WHERE timestamp >= date('now', ?)
              AND NOT EXISTS (
                  SELECT 1 FROM reflection_signal_suppressions AS suppressed
                  WHERE suppressed.signal_id = reflection_signals.id
              )
            ORDER BY timestamp DESC
        """,
            (f"-{days} days",),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_reflection_signals_since(self, since_iso: str, limit: int = 1000) -> List[Dict]:
        """获取自指定 ISO 时间戳以来的 Layer 5 反射信号（供增量画像分析使用）。"""
        if not since_iso:
            return []
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        cursor = conn.execute(
            """
            SELECT * FROM reflection_signals
            WHERE timestamp > ?
              AND NOT EXISTS (
                  SELECT 1 FROM reflection_signal_suppressions AS suppressed
                  WHERE suppressed.signal_id = reflection_signals.id
              )
            ORDER BY timestamp DESC LIMIT ?
            """,
            (since_iso, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    # ---- Note Signals ----

    def insert_note_signal(self, signal: NoteSignal) -> int:
        """插入笔记信号。信号表与metadata表在同一事务中写入。"""
        data = asdict(signal)
        conn = self._pool.get_conn()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO note_signals (
                note_uid, timestamp, content_length,
                has_title, has_list, has_code_block, has_link, image_count,
                tag_count, tags_json, is_ai_generated, ai_agent
            ) VALUES (
                :note_uid, :timestamp, :content_length,
                :has_title, :has_list, :has_code_block, :has_link, :image_count,
                :tag_count, :tags_json, :is_ai_generated, :ai_agent
            )
        """,
            data,
        )
        signal_id = cursor.lastrowid
        if cursor.rowcount == 0 or signal_id is None:
            existing = conn.execute(
                "SELECT id FROM note_signals WHERE note_uid = ?", (data["note_uid"],)
            ).fetchone()
            return existing[0] if existing else 0

        conn.execute(
            """
            INSERT INTO signal_metadata (signal_table, signal_id, confidence, processed)
            VALUES (?, ?, ?, ?)
        """,
            ("notes", signal_id, 0.8 if not signal.is_ai_generated else 0.5, 0),
        )
        conn.commit()
        return signal_id

    def insert_wechat_signal(self, signal: WechatSignal) -> int:
        """插入微信聊天信号。信号表与metadata表在同一事务中写入。"""
        data = asdict(signal)
        topic_tags_json = json.dumps(data.get("topic_tags") or [], ensure_ascii=False)
        conn = self._pool.get_conn()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO wechat_signals (
                timestamp, content_hash, msg_length, has_sensitive_content,
                emotional_valence, emotional_arousal, topic_tags,
                chat_type, hour_of_day, day_of_week, msg_sequence_in_day
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data["timestamp"],
                data["content_hash"],
                data["msg_length"],
                int(data["has_sensitive_content"]),
                data["emotional_valence"],
                data["emotional_arousal"],
                topic_tags_json,
                data["chat_type"],
                data["hour_of_day"],
                data["day_of_week"],
                data["msg_sequence_in_day"],
            ),
        )
        signal_id = cursor.lastrowid
        if cursor.rowcount == 0 or signal_id is None:
            existing = conn.execute(
                "SELECT id FROM wechat_signals WHERE timestamp = ? AND content_hash = ?",
                (data["timestamp"], data["content_hash"]),
            ).fetchone()
            return existing[0] if existing else 0

        conn.execute(
            """
            INSERT INTO signal_metadata (signal_table, signal_id, confidence, processed)
            VALUES (?, ?, ?, ?)
        """,
            ("wechat", signal_id, 0.8, 0),
        )
        conn.commit()
        return signal_id

    def get_recent_wechat_signals(
        self, days: int = SIGNAL_STORE_DURATION_BUCKET_QUARTER_DAYS
    ) -> List[Dict]:
        """获取最近N天的微信信号"""
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        cursor = conn.execute(
            """
            SELECT * FROM wechat_signals
            WHERE timestamp >= date('now', ?)
            ORDER BY timestamp DESC
        """,
            (f"-{days} days",),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_recent_note_signals(
        self, days: int = SIGNAL_STORE_DURATION_BUCKET_QUARTER_DAYS
    ) -> List[Dict]:
        """获取最近N天的笔记信号"""
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        cursor = conn.execute(
            """
            SELECT * FROM note_signals
            WHERE timestamp >= date('now', ?)
            ORDER BY timestamp DESC
        """,
            (f"-{days} days",),
        )
        return [dict(row) for row in cursor.fetchall()]

    # ---- Git Signals ----

    def insert_git_signal(self, signal: GitSignal) -> int:
        """插入git信号。信号表与metadata表在同一事务中写入。"""
        data = asdict(signal)
        conn = self._pool.get_conn()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO git_signals (
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
        """,
            data,
        )
        signal_id = cursor.lastrowid
        if cursor.rowcount == 0 or signal_id is None:
            existing = conn.execute(
                "SELECT id FROM git_signals WHERE commit_hash = ?", (data["commit_hash"],)
            ).fetchone()
            return existing[0] if existing else 0

        # Git信号可能有外部因素（公司规范）
        confidence = 0.7  # 默认较低，需要外部因素标注
        conn.execute(
            """
            INSERT INTO signal_metadata (
                signal_table, signal_id, confidence, processed, possible_external_factors
            )
            VALUES (?, ?, ?, ?, ?)
        """,
            ("git", signal_id, confidence, 0, json.dumps(["possible_company_policy"])),
        )
        conn.commit()
        return signal_id

    # ---- 通用查询 ----

    ALLOWED_SOURCES = {"session", "git", "notes", "knowledge", "wiki", "file_system", "wechat"}

    def _validate_source(self, source_type: str):
        """校验数据源类型，防止 SQL 注入"""
        if source_type not in self.ALLOWED_SOURCES:
            raise ValueError(f"非法数据源: {source_type}")

    @staticmethod
    def _table_for_source(source_type: str) -> str:
        return {"wiki": "knowledge_signals"}.get(source_type, f"{source_type}_signals")

    def get_unprocessed_signals(self, source_type: str, limit: int = 1000) -> List[Dict]:
        """获取未处理的信号（用于画像分析）"""
        self._validate_source(source_type)
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        table = validate_sql_identifier(self._table_for_source(source_type))
        cursor = conn.execute(
            """
            SELECT s.*, m.confidence, m.possible_external_factors
            FROM {} s
            JOIN signal_metadata m ON m.signal_id = s.id AND m.signal_table = ?
            WHERE m.processed = 0
            ORDER BY s.timestamp ASC
            LIMIT ?
        """.format(table),  # nosec B608: table name validated by validate_sql_identifier
            (source_type, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    def mark_signals_processed(self, source_type: str, signal_ids: List[int]):
        """标记信号已处理"""
        if not signal_ids:
            return
        placeholders = ",".join("?" * len(signal_ids))
        conn = self._pool.get_conn()
        conn.execute(
            f"""
            UPDATE signal_metadata
            SET processed = 1, processed_at = ?
            WHERE signal_table = ? AND signal_id IN ({placeholders})
        """,  # nosec B608: internally generated ? placeholders
            (datetime.now().isoformat(), source_type, *signal_ids),
        )
        conn.commit()

    def insert_knowledge_signal(
        self,
        page_path: str,
        action_type: str,
        timestamp: str,
        tags_added: str = "[]",
        tags_removed: str = "[]",
    ) -> int:
        """插入知识库交互信号。信号表与metadata表在同一事务中写入。"""
        conn = self._pool.get_conn()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_signals (
                page_path, action_type, timestamp,
                tags_added, tags_removed
            ) VALUES (?, ?, ?, ?, ?)
        """,
            (page_path, action_type, timestamp, tags_added, tags_removed),
        )
        signal_id = cursor.lastrowid
        if cursor.rowcount == 0 or signal_id is None:
            existing = conn.execute(
                "SELECT id FROM knowledge_signals WHERE page_path = ? AND timestamp = ? AND action_type = ?",  # noqa: E501
                (page_path, timestamp, action_type),
            ).fetchone()
            return existing[0] if existing else 0
        conn.execute(
            """
            INSERT INTO signal_metadata (signal_table, signal_id, confidence, processed)
            VALUES (?, ?, ?, ?)
        """,
            ("knowledge", signal_id, 0.7, 0),
        )
        conn.commit()
        return signal_id

    def insert_file_system_signal(
        self,
        file_path: str,
        action_type: str,
        timestamp: str,
        file_extension: str = "",
        directory_depth: int = 0,
        project_name: str = "",
        is_in_inbox: int = 0,
        is_versioned: int = 0,
    ) -> int:
        """插入文件系统行为信号。信号表与metadata表在同一事务中写入。"""
        conn = self._pool.get_conn()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO file_system_signals (
                file_path, action_type, timestamp,
                file_extension, directory_depth, project_name,
                is_in_inbox, is_versioned
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                file_path,
                action_type,
                timestamp,
                file_extension,
                directory_depth,
                project_name,
                is_in_inbox,
                is_versioned,
            ),
        )
        signal_id = cursor.lastrowid
        if cursor.rowcount == 0 or signal_id is None:
            existing = conn.execute(
                "SELECT id FROM file_system_signals WHERE file_path = ? AND timestamp = ? AND action_type = ?",  # noqa: E501
                (file_path, timestamp, action_type),
            ).fetchone()
            return existing[0] if existing else 0
        conn.execute(
            """
            INSERT INTO signal_metadata (signal_table, signal_id, confidence, processed)
            VALUES (?, ?, ?, ?)
        """,
            ("file_system", signal_id, 0.6, 0),
        )
        conn.commit()
        return signal_id

    def insert_document_signal(
        self,
        session_id: str,
        filename: str,
        doc_type: str,
        doc_category: str,
        title: str,
        key_topics: str,
        entity_type: str,
        page_count: int,
        import_timestamp: str,
        import_source: str,
        confidence: float = 0.0,
    ) -> int:
        """插入外部文档导入信号。信号表与metadata表在同一事务中写入。"""
        conn = self._pool.get_conn()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO document_signals (
                session_id, filename, doc_type, doc_category,
                title, key_topics, entity_type, page_count,
                import_timestamp, import_source, confidence, processed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                session_id,
                filename,
                doc_type,
                doc_category,
                title,
                key_topics,
                entity_type,
                page_count,
                import_timestamp,
                import_source,
                confidence,
                0,
            ),
        )
        signal_id = cursor.lastrowid
        if cursor.rowcount == 0 or signal_id is None:
            existing = conn.execute(
                "SELECT id FROM document_signals WHERE session_id = ? AND import_timestamp = ?",
                (session_id, import_timestamp),
            ).fetchone()
            return existing[0] if existing else 0
        conn.execute(
            """
            INSERT INTO signal_metadata (signal_table, signal_id, confidence, processed)
            VALUES (?, ?, ?, ?)
        """,
            ("document", signal_id, confidence, 0),
        )
        conn.commit()
        return signal_id

    def get_signal_stats(self, days: int = SIGNAL_STORE_GET_SIGNAL_STATS_DAYS) -> Dict[str, Any]:
        """获取信号统计摘要"""
        stats = {}
        conn = self._pool.get_conn()
        # source -> table name 映射（note_signals 是单数，其余是复数）
        table_map = {
            "session": "session_signals",
            "knowledge": "knowledge_signals",
            "git": "git_signals",
            "file_system": "file_system_signals",
            "notes": "note_signals",
            "wechat": "wechat_signals",
        }
        for source in ["session", "knowledge", "git", "file_system", "notes", "wechat"]:
            cursor = conn.execute(
                f"""
                SELECT COUNT(*) FROM {table_map[source]}
                WHERE timestamp >= date('now', ?)
            """,  # nosec B608: table_map is a fixed internal mapping
                (f"-{days} days",),
            )
            stats[source] = cursor.fetchone()[0]
        return stats

    def get_signal_health(self, days: int = SIGNAL_STORE_GET_SIGNAL_HEALTH_DAYS) -> Dict[str, Dict]:
        """返回四类核心信号健康度；fs 为可选。"""
        stats = self.get_signal_stats(days=days)
        core = {
            "session": {"count": stats.get("session", 0), "min": 10, "weight": 0.35},
            "git": {"count": stats.get("git", 0), "min": 5, "weight": 0.25},
            "wiki": {"count": stats.get("knowledge", 0), "min": 5, "weight": 0.20},
            "notes": {"count": stats.get("notes", 0), "min": CORE, "weight": 0.20},
        }
        for data in core.values():
            data["healthy"] = data["count"] >= data["min"]
        core["file_system"] = {
            "count": stats.get("file_system", 0),
            "min": 0,
            "weight": 0.0,
            "healthy": True,
            "optional": True,
        }
        return core

    def handle_event(self, event_type: str, data: Dict):
        """事件式信号注入入口。"""
        now = data.get("timestamp") or datetime.now().isoformat()
        if event_type == "session_completed":
            try:
                return self.insert_session_signal(
                    SessionSignal(
                        session_id=data.get("session_id", ""),
                        timestamp=now,
                        task_type=data.get("task_type", ""),
                        task_subtype=data.get("task_subtype", ""),
                        follow_up_depth=int(data.get("follow_up_depth", 0) or 0),
                        termination_type=data.get("termination_type", ""),
                        output_type=data.get("output_type", ""),
                        duration_seconds=int(data.get("duration_seconds", 0) or 0),
                        working_dir=data.get("working_dir", ""),
                        agent=data.get("agent", "unknown"),
                    ),
                    session_context=data,
                )
            except (ValueError, TypeError) as e:
                logger.warning("session_completed 信号类型转换失败: %s", e, exc_info=True)
                return 0
        if event_type == "wiki_page_accessed":
            return self.insert_knowledge_signal(
                data.get("page_path", ""),
                data.get("action_type", "access"),
                now,
            )
        if event_type == "notes_synced":
            try:
                return self.insert_note_signal(
                    NoteSignal(
                        note_uid=data.get("note_uid", ""),
                        timestamp=now,
                        content_length=int(data.get("content_length", 0) or 0),
                        has_title=bool(data.get("has_title", False)),
                        has_code_block=bool(data.get("has_code_block", False)),
                        tags_json=json.dumps(data.get("tags", []), ensure_ascii=False),
                    )
                )
            except (ValueError, TypeError) as e:
                logger.warning("notes_synced 信号类型转换失败: %s", e, exc_info=True)
                return 0
        if event_type == "git_commit_detected":
            try:
                return self.insert_git_signal(
                    GitSignal(
                        repo_path=data.get("repo_path", ""),
                        commit_hash=data.get("commit_hash", ""),
                        timestamp=now,
                        message_length=int(data.get("message_length", 0) or 0),
                        commit_type=data.get("commit_type", ""),
                        is_weekend=bool(data.get("is_weekend", False)),
                        hour_of_day=int(data.get("hour_of_day", 12) or 12),
                    )
                )
            except (ValueError, TypeError) as e:
                logger.warning("git_commit_detected 信号类型转换失败: %s", e, exc_info=True)
                return 0
        return None

    def add_signal(
        self,
        dimension: str,
        value: str,
        confidence: float = 0.0,
        source: str = "",
        source_event_id: str = "",
    ) -> int:
        """写入一条 Layer 5 反射信号（供 PersonaSignalConsumer 使用）"""
        return persist_reflection_signal(
            self,
            dimension=dimension,
            value=value,
            confidence=confidence,
            source=source,
            source_event_id=source_event_id,
        )


# ========== 单例 ==========

_signal_store: Optional[SignalStore] = None


def _default_signal_db_path() -> Path:
    if not isinstance(SIGNAL_DB_PATH, LazyPath):
        return Path(SIGNAL_DB_PATH).expanduser()
    try:
        from core.config import get_config

        config = get_config()
    except (ImportError, OSError, RuntimeError, AttributeError, ValueError, TypeError, KeyError):
        config = None
    for attr_name in ("database_dir", "data_dir"):
        candidate = getattr(config, attr_name, None) if config is not None else None
        if isinstance(candidate, Mock) or not isinstance(candidate, (str, PathLike)):
            continue
        base = Path(candidate).expanduser()
        if "MagicMock" in str(base):
            continue
        return base / "user_signals.db"
    return Path.home() / ".mnemos" / "user_signals.db"


def _same_signal_store_path(store: SignalStore, db_path: Path) -> bool:
    try:
        current = Path(store.db_path).expanduser()
    except (TypeError, ValueError, AttributeError):
        return False
    try:
        return current.resolve(strict=False) == db_path.resolve(strict=False)
    except OSError:
        return current == db_path


def get_signal_store() -> SignalStore:
    """获取SignalStore单例"""
    global _signal_store
    db_path = _default_signal_db_path()
    stale = False
    if _signal_store is not None:
        stale = not _same_signal_store_path(_signal_store, db_path)
        if not stale and not sqlite_artifact_exists(db_path):
            stale = True
    if stale:
        old_store = _signal_store
        try:
            if old_store is not None:
                old_store.close()
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError, sqlite3.Error):
            logger.warning("Failed to close stale SignalStore singleton", exc_info=True)
        _signal_store = None
    if _signal_store is None:
        _signal_store = SignalStore(db_path=db_path)
    return _signal_store


def suppress_reflection_signal(
    *,
    signal_id: int,
    source_event_id: str,
    reason: str,
    evidence: Dict[str, Any],
) -> Dict[str, str]:
    """Append an exact suppression receipt and prove the signal is inactive."""

    if signal_id <= 0 or not source_event_id or not reason:
        raise ValueError("reflection signal suppression identity is incomplete")
    store = get_signal_store()
    conn = store._pool.get_conn()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        signal = conn.execute(
            "SELECT * FROM reflection_signals WHERE id=?",
            (signal_id,),
        ).fetchone()
        if signal is None:
            raise ValueError("reflection signal suppression target is missing")
        signal_payload = dict(signal)
        before_hash = sha256_json(
            {
                "schema_version": "mnemos.reflection_signal_state.v1",
                "signal": signal_payload,
                "active": True,
            }
        )
        after_hash = sha256_json(
            {
                "schema_version": "mnemos.reflection_signal_state.v1",
                "signal": signal_payload,
                "active": False,
                "suppression_source_event_id": source_event_id,
            }
        )
        suppression_id = sha256_json(
            {
                "schema_version": "mnemos.reflection_signal_suppression.v1",
                "signal_id": signal_id,
                "source_event_id": source_event_id,
                "reason": reason,
                "before_hash": before_hash,
                "after_hash": after_hash,
            }
        )
        existing = conn.execute(
            """
            SELECT suppression_id, signal_id, before_hash, after_hash
            FROM reflection_signal_suppressions
            WHERE source_event_id=?
            """,
            (source_event_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO reflection_signal_suppressions (
                    suppression_id, signal_id, source_event_id, reason,
                    evidence_json, before_hash, after_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    suppression_id,
                    signal_id,
                    source_event_id,
                    reason,
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    before_hash,
                    after_hash,
                    datetime.now().astimezone().isoformat(),
                ),
            )
        elif (
            str(existing["suppression_id"]) != suppression_id
            or int(existing["signal_id"]) != signal_id
            or str(existing["before_hash"]) != before_hash
            or str(existing["after_hash"]) != after_hash
        ):
            raise ValueError("reflection signal suppression replay conflicts")
        active = conn.execute(
            """
            SELECT 1 FROM reflection_signals AS signal
            WHERE signal.id=?
              AND NOT EXISTS (
                  SELECT 1 FROM reflection_signal_suppressions AS suppressed
                  WHERE suppressed.signal_id=signal.id
              )
            """,
            (signal_id,),
        ).fetchone()
        if active is not None:
            raise RuntimeError("reflection signal remained active after suppression")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        release = getattr(store._pool, "release_transient_connections", None)
        if callable(release):
            release()
    return {
        "receipt_ref": f"reflection-signal-suppression:{suppression_id}",
        "target_oracle": f"reflection-signal:{signal_id}:inactive:{after_hash}",
        "before_hash": before_hash,
        "after_hash": after_hash,
    }


# ========== 便捷函数 ==========


def log_session_signal(**kwargs) -> int:
    """便捷函数：记录session信号"""
    signal = SessionSignal(**kwargs)
    return get_signal_store().insert_session_signal(signal)


def get_recent_signals_summary(days: int = DURATION_BUCKET_WEEK_DAYS) -> str:
    """获取最近信号摘要（用于调试）"""
    store = get_signal_store()
    stats = store.get_signal_stats(days=days)
    lines = [f"📊 最近{days}天信号统计:"]
    for source, count in stats.items():
        lines.append(f"  {source}: {count}")
    total = sum(stats.values())
    lines.append(f"  总计: {total}")
    return "\n".join(lines)


def build_persona_feedback_proposal_owner(database_dir: Path):
    """Return the persona-owned pending-review journal for feedback commands."""

    from core.cognitive.feedback_target_registry import (
        build_registered_feedback_proposal_owner,
    )

    return build_registered_feedback_proposal_owner(database_dir, "persona_proposal")


if __name__ == "__main__":
    # 测试
    store = SignalStore()
    print("✅ SignalStore initialized")
    print(f"   Database: {store.db_path}")
    print(get_recent_signals_summary(days=7))
