import hashlib
import logging

"""
Reflection Store — SQLite 持久化层

存储内容：
- ReflectionRecord: Reflection 生成过程的元数据
- CognitiveShift: 认知变迁事件
- UserFeedback: 用户反馈

注意：不存储 Insight 全文（运行时生成），只存储摘要和元数据
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_access,
    cognitive_access_hash,
    cognitive_access_matches_subject,
    validate_cognitive_access_envelope,
)
from core.db_utils import delete_older_than
from core.reflection.models import (
    CognitiveShift,
    FeedbackType,
    ImplicitFeedbackRecord,
    InsightSnapshot,
    MirrorSnapshot,
    ReflectionRecord,
    ReflectionTrigger,
    UserFeedback,
)
from core.reflection.feedback_provenance import ACTIVE_COGNITIVE_SHIFT_SQL
from core.reflection.feedback_provenance import ACTIVE_LAYER5_EXPERIENCE_SQL
from core.reflection.reflection_access import (  # noqa: F401
    REFLECTION_DELETION_SCHEMA_VERSION,
    REFLECTION_DELETION_TABLE,
    REFLECTION_OBJECT_PURPOSES,
    _deletion_scope_hash,
    _load_reflection_access,
    _reflection_deletion_receipt_id,
    _reflection_deletion_sql,
    _restricted_reflection_access,
    derive_reflection_access,
    normalize_reflection_access,
)

logger = logging.getLogger(__name__)


class ReflectionStore:
    """Reflection SQLite 存储"""

    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        ownership_config: Any | None = None,
        initialize: bool = True,
        read_only: bool = False,
    ):
        if db_path:
            self.db_path = Path(db_path)
        else:
            self.db_path = Path.home() / ".mnemos" / "reflections.db"
        if ownership_config is None:
            from core.config import get_config

            ownership_config = get_config()
        self._ownership_config = ownership_config
        self.read_only = bool(read_only)
        if initialize:
            if self.read_only:
                raise ValueError("read-only ReflectionStore cannot initialize schema")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def _read_connection(self) -> sqlite3.Connection:
        if self.read_only:
            if not self.db_path.is_file():
                raise FileNotFoundError(self.db_path)
            return sqlite3.connect(
                f"file:{self.db_path.resolve(strict=True)}?mode=ro",
                uri=True,
            )
        return sqlite3.connect(self.db_path)

    def _assert_writable(self) -> None:
        """Reject every mutation through a projection-replay store."""

        if self.read_only:
            raise PermissionError("read-only ReflectionStore cannot mutate canonical state")

    def _assert_write_not_frozen(
        self,
        access_control: Mapping[str, Any],
        *,
        source_event_ids: tuple[str, ...] = (),
    ) -> None:
        from core.privacy.ownership_freeze import cognitive_write_is_frozen

        scope = access_control["scope"]
        if cognitive_write_is_frozen(
            self._ownership_config,
            session_id=str(scope.get("session_id") or ""),
            project=str(scope.get("project") or ""),
            agent=str(access_control["owner"].get("agent") or ""),
            source_event_ids=source_event_ids,
        ):
            raise PermissionError(
                "reflection write is blocked by a matching frozen data ownership scope"
            )

    def _init_db(self):
        """初始化数据库表结构"""
        with self._read_connection() as conn:
            # Reflection 记录主表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reflection_records (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    trigger_event TEXT DEFAULT '',
                    user_query TEXT DEFAULT '',
                    mirror_snapshots TEXT NOT NULL,  -- JSON
                    mirror_dimensions TEXT NOT NULL,  -- JSON list
                    insight_summary TEXT DEFAULT '',
                    insight_key_points TEXT DEFAULT '[]',  -- JSON list
                    insight_dimensions TEXT DEFAULT '[]',  -- JSON list
                    temporal_context TEXT DEFAULT '{}',  -- JSON
                    feedback_type TEXT,
                    feedback_comment TEXT DEFAULT '',
                    feedback_given_at TEXT,
                    fed_back_to_observations INTEGER DEFAULT 0,
                    fed_back_to_knowledge INTEGER DEFAULT 0,
                    implicit_feedback_type TEXT,
                    implicit_feedback_confidence REAL,
                    implicit_feedback_signals TEXT DEFAULT '[]',  -- JSON list
                    implicit_feedback_at TEXT,
                    internal_validation TEXT DEFAULT '{}',  -- JSON
                    access_control TEXT NOT NULL DEFAULT ''  -- canonical object ACL JSON
                )
            """)

            # 认知变迁表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cognitive_shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dimension TEXT NOT NULL,
                    shift_type TEXT NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    confidence REAL DEFAULT 0.0,
                    evidence TEXT DEFAULT '[]',  -- JSON list
                    first_seen_at TEXT,
                    shift_detected_at TEXT NOT NULL,
                    related_reflection_id TEXT,
                    source_event_id TEXT NOT NULL DEFAULT '',
                    access_control TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (related_reflection_id) REFERENCES reflection_records(id)
                )
            """)

            # 索引
            # Explicit bootstrap ensures the current reflection columns.
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(reflection_records)")
            }
            for col_name, col_def in (
                ("implicit_feedback_type", "TEXT"),
                ("implicit_feedback_confidence", "REAL"),
                ("implicit_feedback_signals", "TEXT DEFAULT '[]'"),
                ("implicit_feedback_at", "TEXT"),
                ("internal_validation", "TEXT DEFAULT '{}'"),
                ("access_control", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE reflection_records ADD COLUMN {col_name} {col_def}")

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_reflection_created
                ON reflection_records(created_at DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_reflection_trigger
                ON reflection_records(trigger)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_shift_dimension
                ON cognitive_shifts(dimension)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_shift_detected
                ON cognitive_shifts(shift_detected_at DESC)
            """)

            # Layer 5 经验库（供 KIAExperienceConsumer 使用）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS layer5_experiences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    dimension TEXT,
                    dimensions TEXT DEFAULT '[]',
                    trigger TEXT,
                    confidence REAL DEFAULT 0.0,
                    summary TEXT,
                    reason TEXT,
                    from_state TEXT,
                    to_state TEXT,
                    evidence TEXT DEFAULT '[]',
                    timestamp TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    source_event_id TEXT NOT NULL DEFAULT '',
                    access_control TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {REFLECTION_DELETION_TABLE} (
                    receipt_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    scope_kind TEXT NOT NULL,
                    scope_value_hash TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    before_acl_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status='applied'),
                    created_at TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    UNIQUE(object_type, object_id)
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_reflection_deletion_request
                ON {REFLECTION_DELETION_TABLE}(request_id, status)
                """
            )
            shift_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(cognitive_shifts)")
            }
            if "source_event_id" not in shift_columns:
                conn.execute(
                    "ALTER TABLE cognitive_shifts ADD COLUMN source_event_id TEXT NOT NULL DEFAULT ''"
                )
            if "access_control" not in shift_columns:
                conn.execute(
                    "ALTER TABLE cognitive_shifts ADD COLUMN access_control TEXT NOT NULL DEFAULT ''"
                )
            experience_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(layer5_experiences)")
            }
            if "source_event_id" not in experience_columns:
                conn.execute(
                    "ALTER TABLE layer5_experiences ADD COLUMN source_event_id TEXT NOT NULL DEFAULT ''"
                )
            if "access_control" not in experience_columns:
                conn.execute(
                    "ALTER TABLE layer5_experiences ADD COLUMN access_control TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_shift_source_event
                ON cognitive_shifts(source_event_id)
                WHERE source_event_id <> ''
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_layer5_source_event
                ON layer5_experiences(source_event_id)
                WHERE source_event_id <> ''
                """
            )
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_layer5_experience_type
                ON layer5_experiences(type)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_layer5_experience_dim
                ON layer5_experiences(dimension)
            """)
            conn.commit()

    def save_record(self, record: ReflectionRecord) -> bool:
        """保存 Reflection 记录"""
        self._assert_writable()
        access_control = normalize_reflection_access(
            record.access_control,
            object_ref=record.id,
        )
        self._assert_write_not_frozen(
            access_control,
            source_event_ids=tuple(
                value for value in (str(record.trigger_event or ""),) if value
            ),
        )
        record.access_control = dict(access_control)
        with self._read_connection() as conn:
            deleted = conn.execute(
                _reflection_deletion_sql(
                    """
                SELECT 1 FROM {reflection_deletion_table}
                WHERE object_type='reflection_record' AND object_id=?
                """
                ),
                (record.id,),
            ).fetchone()
            if deleted is not None:
                raise PermissionError("reflection record is subject-deleted and cannot be restored")
            conn.execute(
                """INSERT OR REPLACE INTO reflection_records (
                    id, created_at, trigger, trigger_event, user_query,
                    mirror_snapshots, mirror_dimensions, insight_summary,
                    insight_key_points, insight_dimensions, temporal_context,
                    feedback_type, feedback_comment, feedback_given_at,
                    fed_back_to_observations, fed_back_to_knowledge,
                    implicit_feedback_type, implicit_feedback_confidence,
                    implicit_feedback_signals, implicit_feedback_at,
                    internal_validation, access_control
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.created_at.isoformat(),
                    record.trigger.value,
                    record.trigger_event,
                    record.user_query,
                    json.dumps(
                        [s.__dict__ for s in record.mirror_snapshots],
                        ensure_ascii=False,
                        default=str,
                    ),
                    json.dumps(record.mirror_dimensions, ensure_ascii=False, default=str),
                    record.insight.summary if record.insight else "",
                    json.dumps(
                        record.insight.key_points if record.insight else [],
                        ensure_ascii=False,
                        default=str,
                    ),
                    json.dumps(
                        record.insight.dimensions_involved if record.insight else [],
                        ensure_ascii=False,
                        default=str,
                    ),
                    json.dumps(record.temporal_context or {}, ensure_ascii=False, default=str),
                    record.user_feedback.feedback_type.value if record.user_feedback else None,
                    record.user_feedback.comment if record.user_feedback else "",
                    record.user_feedback.given_at.isoformat() if record.user_feedback else None,
                    1 if record.fed_back_to_observations else 0,
                    1 if record.fed_back_to_knowledge else 0,
                    # 隐式反馈
                    (
                        record.implicit_feedback.inferred_type.value
                        if record.implicit_feedback
                        else None
                    ),
                    record.implicit_feedback.confidence if record.implicit_feedback else None,
                    json.dumps(
                        record.implicit_feedback.signals if record.implicit_feedback else [],
                        ensure_ascii=False,
                        default=str,
                    ),
                    (
                        record.implicit_feedback.inferred_at.isoformat()
                        if record.implicit_feedback
                        else None
                    ),
                    # 内部校验
                    json.dumps(record.internal_validation or {}, ensure_ascii=False, default=str),
                    json.dumps(access_control, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
            conn.commit()
        return True

    def save_shift(
        self,
        shift: CognitiveShift,
        reflection_id: Optional[str] = None,
        *,
        source_event_id: str = "",
    ):
        """保存认知变迁"""
        self._assert_writable()
        with self._read_connection() as conn:
            if reflection_id:
                deleted = conn.execute(
                    _reflection_deletion_sql(
                        """
                    SELECT 1 FROM {reflection_deletion_table}
                    WHERE object_type='reflection_record' AND object_id=?
                    """
                    ),
                    (reflection_id,),
                ).fetchone()
                if deleted is not None:
                    raise PermissionError(
                        "reflection record is subject-deleted and cannot receive a shift"
                    )
            access_control = normalize_reflection_access(
                shift.access_control,
                object_ref=f"shift:{shift.dimension}:{shift.shift_detected_at.isoformat()}",
            )
            if not shift.access_control and reflection_id:
                source_row = conn.execute(
                    "SELECT access_control FROM reflection_records WHERE id=?",
                    (reflection_id,),
                ).fetchone()
                if source_row is not None:
                    access_control = _load_reflection_access(
                        source_row[0],
                        object_ref=f"reflection:{reflection_id}",
                    )
            self._assert_write_not_frozen(
                access_control,
                source_event_ids=tuple(
                    value for value in (str(source_event_id or ""),) if value
                ),
            )
            shift.access_control = dict(access_control)
            shift.related_reflection_id = str(reflection_id or "")
            conn.execute(
                """INSERT OR IGNORE INTO cognitive_shifts
                    (dimension, shift_type, from_state, to_state, confidence,
                     evidence, first_seen_at, shift_detected_at, related_reflection_id,
                     source_event_id, access_control)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    shift.dimension,
                    shift.shift_type,
                    shift.from_state,
                    shift.to_state,
                    shift.confidence,
                    json.dumps(shift.evidence, ensure_ascii=False),
                    shift.first_seen_at.isoformat() if shift.first_seen_at else None,
                    shift.shift_detected_at.isoformat(),
                    reflection_id,
                    source_event_id,
                    json.dumps(access_control, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
            conn.commit()

    def add_experience(self, experience: Dict) -> int:
        """写入一条 Layer 5 经验（供 KIAExperienceConsumer 使用）"""
        self._assert_writable()
        source_event_id = str(experience.get("source_event_id") or "")
        object_ref = "layer5:" + (
            source_event_id
            or hashlib.sha256(
                json.dumps(
                    experience,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()[:24]
        )
        access_control = normalize_reflection_access(
            experience.get("access_control"),
            object_ref=object_ref,
        )
        self._assert_write_not_frozen(
            access_control,
            source_event_ids=tuple(value for value in (source_event_id,) if value),
        )
        with self._read_connection() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO layer5_experiences (
                    type, dimension, dimensions, trigger, confidence, summary,
                    reason, from_state, to_state, evidence, timestamp, source_event_id,
                    access_control
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experience.get("type", ""),
                    experience.get("dimension"),
                    json.dumps(experience.get("dimensions", []), ensure_ascii=False),
                    experience.get("trigger", ""),
                    experience.get("confidence", 0.0),
                    experience.get("summary", ""),
                    experience.get("reason", ""),
                    experience.get("from_state", ""),
                    experience.get("to_state", ""),
                    json.dumps(experience.get("evidence", []), ensure_ascii=False),
                    datetime.now().isoformat(),
                    source_event_id,
                    json.dumps(
                        access_control,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            conn.commit()
            if cursor.rowcount:
                return cursor.lastrowid or 0
            if source_event_id:
                row = conn.execute(
                    "SELECT id FROM layer5_experiences WHERE source_event_id=?",
                    (source_event_id,),
                ).fetchone()
                return int(row[0]) if row else 0
            return 0

    def get_experiences(
        self,
        type: Optional[str] = None,
        dimension: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        """读取 Layer 5 经验库（供 preflight/guard 回流消费）"""
        conditions: List[str] = [ACTIVE_LAYER5_EXPERIENCE_SQL]
        params: List[Any] = []
        if type:
            conditions.append("type = ?")
            params.append(type)
        if dimension:
            conditions.append("dimension = ?")
            params.append(dimension)

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"""
            SELECT id, type, dimension, dimensions, trigger, confidence, summary,
                   reason, from_state, to_state, evidence, timestamp
            FROM layer5_experiences
            {where_clause}
            ORDER BY timestamp DESC
            LIMIT ?
        """  # nosec B608
        params.append(limit)

        try:
            with self._read_connection() as conn:
                conn.row_factory = sqlite3.Row  # noqa
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            logger.warning("[reflection_store] 读取 layer5_experiences 失败: %s", e)
            return []

        return [self._experience_from_row(row) for row in rows]

    @staticmethod
    def _experience_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        try:
            dimensions = json.loads(row["dimensions"]) if row["dimensions"] else []
        except json.JSONDecodeError:
            dimensions = []
        try:
            evidence = json.loads(row["evidence"]) if row["evidence"] else []
        except json.JSONDecodeError:
            evidence = []
        return {
            "id": row["id"],
            "type": row["type"] or "",
            "dimension": row["dimension"] or "",
            "dimensions": dimensions,
            "trigger": row["trigger"] or "",
            "confidence": row["confidence"] or 0.0,
            "summary": row["summary"] or "",
            "reason": row["reason"] or "",
            "from_state": row["from_state"] or "",
            "to_state": row["to_state"] or "",
            "evidence": evidence,
            "timestamp": row["timestamp"] or "",
        }

    def authorized_get_experiences(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
        type: Optional[str] = None,
        dimension: Optional[str] = None,
        limit: int = 50,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Authorize Layer-5 headers before hydrating experience bodies."""

        if principal is None:
            return [], {
                "candidate_count": 0,
                "authorized_count": 0,
                "denied_by_reason": {"principal_required": 1},
            }
        conditions: List[str] = [ACTIVE_LAYER5_EXPERIENCE_SQL]
        params: List[Any] = []
        if type:
            conditions.append("type=?")
            params.append(type)
        if dimension:
            conditions.append("dimension=?")
            params.append(dimension)
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        denied_by_reason: Dict[str, int] = {}
        authorized_ids: List[int] = []
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            headers = conn.execute(
                f"SELECT id, access_control FROM layer5_experiences "
                f"{where_clause} ORDER BY timestamp DESC",  # nosec B608 - fixed clauses
                tuple(params),
            ).fetchall()
            for header in headers:
                reason = self._authorize_record_header(
                    header["access_control"],
                    principal=principal,
                    narrowing=narrowing,
                    purpose=purpose,
                )
                if reason == "authorized":
                    authorized_ids.append(int(header["id"]))
                    if len(authorized_ids) >= max(0, int(limit)):
                        break
                else:
                    denied_by_reason[reason] = denied_by_reason.get(reason, 0) + 1
            if not authorized_ids:
                return [], {
                    "candidate_count": len(headers),
                    "authorized_count": 0,
                    "denied_by_reason": denied_by_reason,
                }
            placeholders = ",".join("?" for _ in authorized_ids)
            rows_by_id = {
                int(row["id"]): row
                for row in conn.execute(
                    f"SELECT * FROM layer5_experiences "
                    f"WHERE id IN ({placeholders})",  # nosec B608 - placeholders only
                    tuple(authorized_ids),
                ).fetchall()
            }
        return (
            [
                self._experience_from_row(rows_by_id[item_id])
                for item_id in authorized_ids
                if item_id in rows_by_id
            ],
            {
                "candidate_count": len(headers),
                "authorized_count": len(authorized_ids),
                "denied_by_reason": denied_by_reason,
            },
        )

    @staticmethod
    def _authorize_record_header(
        raw_access_control: Any,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
    ) -> str:
        """Authorize a persisted ACL header before reading reflection content."""

        try:
            access_control = validate_cognitive_access_envelope(
                json.loads(str(raw_access_control or ""))
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return "acl_unknown"
        return authorize_cognitive_access(
            access_control,
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
        ).reason

    def authorized_get_latest(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
        limit: int = 10,
    ) -> tuple[List[ReflectionRecord], Dict[str, Any]]:
        """Return only ACL-authorized records, checking headers before bodies."""

        if principal is None:
            return [], {
                "candidate_count": 0,
                "authorized_count": 0,
                "denied_by_reason": {"principal_required": 1},
            }
        if not str(purpose or "").strip():
            return [], {
                "candidate_count": 0,
                "authorized_count": 0,
                "denied_by_reason": {"purpose_required": 1},
            }
        denied_by_reason: Dict[str, int] = {}
        authorized_ids: List[str] = []
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            headers = conn.execute(
                "SELECT id, access_control FROM reflection_records ORDER BY created_at DESC"
            ).fetchall()
            for header in headers:
                reason = self._authorize_record_header(
                    header["access_control"],
                    principal=principal,
                    narrowing=narrowing,
                    purpose=purpose,
                )
                if reason == "authorized":
                    authorized_ids.append(str(header["id"]))
                    if len(authorized_ids) >= max(0, int(limit)):
                        break
                else:
                    denied_by_reason[reason] = denied_by_reason.get(reason, 0) + 1
            if not authorized_ids:
                return [], {
                    "candidate_count": len(headers),
                    "authorized_count": 0,
                    "denied_by_reason": denied_by_reason,
                }
            placeholders = ",".join("?" for _ in authorized_ids)
            rows_by_id = {
                str(row["id"]): row
                for row in conn.execute(
                    f"SELECT * FROM reflection_records "
                    f"WHERE id IN ({placeholders})",  # nosec B608 - generated placeholders
                    tuple(authorized_ids),
                ).fetchall()
            }
        return (
            [self._row_to_record(rows_by_id[record_id]) for record_id in authorized_ids if record_id in rows_by_id],
            {
                "candidate_count": len(headers),
                "authorized_count": len(authorized_ids),
                "denied_by_reason": denied_by_reason,
            },
        )

    def authorized_get_by_id(
        self,
        reflection_id: str,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
    ) -> tuple[Optional[ReflectionRecord], Dict[str, int]]:
        """Read one ReflectionRecord only after its ACL header authorizes it."""

        if principal is None:
            return None, {"principal_required": 1}
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            header = conn.execute(
                "SELECT id, access_control FROM reflection_records WHERE id=?",
                (reflection_id,),
            ).fetchone()
            if header is None:
                return None, {"not_found": 1}
            reason = self._authorize_record_header(
                header["access_control"],
                principal=principal,
                narrowing=narrowing,
                purpose=purpose,
            )
            if reason != "authorized":
                return None, {reason: 1}
            row = conn.execute(
                "SELECT * FROM reflection_records WHERE id=?",
                (reflection_id,),
            ).fetchone()
        return (self._row_to_record(row) if row is not None else None), {"authorized": 1}

    def get_latest(self, limit: int = 10) -> List[ReflectionRecord]:
        """获取最新的 Reflection 记录"""
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row  # noqa
            rows = conn.execute(
                "SELECT * FROM reflection_records ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_all_for_projection(self) -> List[ReflectionRecord]:
        """Return every committed Reflection for lossless projection replay."""

        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM reflection_records ORDER BY created_at DESC, id ASC"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_by_id(self, reflection_id: str) -> Optional[ReflectionRecord]:
        """按 ID 获取单条 Reflection 记录（使用主键索引，避免全表扫描）。"""
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row  # noqa
            row = conn.execute(
                "SELECT * FROM reflection_records WHERE id = ?",
                (reflection_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_record(row)

    def get_by_trigger(self, trigger: ReflectionTrigger, limit: int = 20) -> List[ReflectionRecord]:
        """按触发类型查询"""
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row  # noqa
            rows = conn.execute(
                "SELECT * FROM reflection_records WHERE trigger = ? ORDER BY created_at DESC LIMIT ?",  # noqa: E501
                (trigger.value, limit),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def authorized_get_by_trigger(
        self,
        trigger: ReflectionTrigger,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
        limit: int = 20,
    ) -> tuple[List[ReflectionRecord], Dict[str, Any]]:
        """Get same-trigger records through the header-first ACL seam."""

        if principal is None:
            return [], {
                "candidate_count": 0,
                "authorized_count": 0,
                "denied_by_reason": {"principal_required": 1},
            }
        denied_by_reason: Dict[str, int] = {}
        authorized_ids: List[str] = []
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            headers = conn.execute(
                """SELECT id, access_control FROM reflection_records
                   WHERE trigger=? ORDER BY created_at DESC""",
                (trigger.value,),
            ).fetchall()
            for header in headers:
                reason = self._authorize_record_header(
                    header["access_control"],
                    principal=principal,
                    narrowing=narrowing,
                    purpose=purpose,
                )
                if reason == "authorized":
                    authorized_ids.append(str(header["id"]))
                    if len(authorized_ids) >= max(0, int(limit)):
                        break
                else:
                    denied_by_reason[reason] = denied_by_reason.get(reason, 0) + 1
            if not authorized_ids:
                return [], {
                    "candidate_count": len(headers),
                    "authorized_count": 0,
                    "denied_by_reason": denied_by_reason,
                }
            placeholders = ",".join("?" for _ in authorized_ids)
            rows_by_id = {
                str(row["id"]): row
                for row in conn.execute(
                    f"SELECT * FROM reflection_records "
                    f"WHERE id IN ({placeholders})",  # nosec B608 - generated placeholders
                    tuple(authorized_ids),
                ).fetchall()
            }
        return (
            [self._row_to_record(rows_by_id[record_id]) for record_id in authorized_ids if record_id in rows_by_id],
            {
                "candidate_count": len(headers),
                "authorized_count": len(authorized_ids),
                "denied_by_reason": denied_by_reason,
            },
        )

    def get_shifts(self, dimension: Optional[str] = None, limit: int = 50) -> List[CognitiveShift]:
        """获取认知变迁记录"""
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row  # noqa
            if dimension:
                rows = conn.execute(
                    f"SELECT * FROM cognitive_shifts WHERE dimension = ? "
                    f"AND {ACTIVE_COGNITIVE_SHIFT_SQL} "
                    "ORDER BY shift_detected_at DESC LIMIT ?",  # nosec B608
                    (dimension, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM cognitive_shifts WHERE "
                    f"{ACTIVE_COGNITIVE_SHIFT_SQL} "
                    "ORDER BY shift_detected_at DESC LIMIT ?",  # nosec B608
                    (limit,),
                ).fetchall()
        return [self._row_to_shift(row) for row in rows]

    def get_all_shifts_for_projection(self) -> List[CognitiveShift]:
        """Return the complete active shift denominator for projection replay."""

        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM cognitive_shifts WHERE {ACTIVE_COGNITIVE_SHIFT_SQL} "
                "ORDER BY shift_detected_at DESC, id ASC"  # nosec B608
            ).fetchall()
        return [self._row_to_shift(row) for row in rows]

    def authorized_get_shifts(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
        dimension: Optional[str] = None,
        limit: int = 50,
    ) -> tuple[List[CognitiveShift], Dict[str, Any]]:
        """Read shift bodies only after their object ACL headers authorize."""

        if principal is None:
            return [], {
                "candidate_count": 0,
                "authorized_count": 0,
                "denied_by_reason": {"principal_required": 1},
            }
        where = (
            f"WHERE dimension=? AND {ACTIVE_COGNITIVE_SHIFT_SQL}"
            if dimension
            else f"WHERE {ACTIVE_COGNITIVE_SHIFT_SQL}"
        )
        params: tuple[Any, ...] = (dimension,) if dimension else ()
        denied_by_reason: Dict[str, int] = {}
        authorized_ids: List[int] = []
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            headers = conn.execute(
                f"SELECT id, access_control FROM cognitive_shifts {where} "
                "ORDER BY shift_detected_at DESC",  # nosec B608 - fixed where clause
                params,
            ).fetchall()
            for header in headers:
                reason = self._authorize_record_header(
                    header["access_control"],
                    principal=principal,
                    narrowing=narrowing,
                    purpose=purpose,
                )
                if reason == "authorized":
                    authorized_ids.append(int(header["id"]))
                    if len(authorized_ids) >= max(0, int(limit)):
                        break
                else:
                    denied_by_reason[reason] = denied_by_reason.get(reason, 0) + 1
            if not authorized_ids:
                return [], {
                    "candidate_count": len(headers),
                    "authorized_count": 0,
                    "denied_by_reason": denied_by_reason,
                }
            placeholders = ",".join("?" for _ in authorized_ids)
            rows_by_id = {
                int(row["id"]): row
                for row in conn.execute(
                    f"SELECT * FROM cognitive_shifts "
                    f"WHERE id IN ({placeholders})",  # nosec B608 - generated placeholders
                    tuple(authorized_ids),
                ).fetchall()
            }
        return (
            [self._row_to_shift(rows_by_id[shift_id]) for shift_id in authorized_ids if shift_id in rows_by_id],
            {
                "candidate_count": len(headers),
                "authorized_count": len(authorized_ids),
                "denied_by_reason": denied_by_reason,
            },
        )

    def add_feedback(self, reflection_id: str, feedback: UserFeedback):
        """Reject the retired direct writer; history remains queryable."""

        del reflection_id, feedback
        raise RuntimeError("legacy_reflection_feedback_write_retired")

    def mark_fed_back(
        self, reflection_id: str, to_observations: bool = False, to_knowledge: bool = False
    ):
        """标记反哺状态"""
        self._assert_writable()
        with self._read_connection() as conn:
            if to_observations:
                conn.execute(
                    "UPDATE reflection_records SET fed_back_to_observations = 1 WHERE id = ?",
                    (reflection_id,),
                )
            if to_knowledge:
                conn.execute(
                    "UPDATE reflection_records SET fed_back_to_knowledge = 1 WHERE id = ?",
                    (reflection_id,),
                )
            conn.commit()

    def delete_subject_scope(
        self,
        *,
        request_id: str,
        scope_kind: str,
        scope_value: str,
    ) -> Dict[str, Any]:
        """Delete ACL-provable Reflection objects and write typed receipts.

        Scoped deletion matches only canonical ACL headers; it never searches a
        query, insight, mirror, evidence, or feedback body for a subject
        literal. Unmapped historical Layer-5 rows carry no ACL, so scoped deletion
        leaves them untouched and reports the unresolved residual instead of
        deleting a potentially unrelated subject or claiming verification.
        """

        self._assert_writable()

        kind = str(scope_kind or "").strip().lower()
        value = str(scope_value or "").strip()
        supported_scopes = {"all", "agent", "session", "project"}
        if kind not in supported_scopes:
            return {
                "status": "unsupported_scope",
                "target_count": 0,
                "supported_scopes": sorted(supported_scopes),
                "verified": False,
            }
        if kind == "all" and value != "all":
            return {
                "status": "unsupported_scope",
                "target_count": 0,
                "supported_scopes": sorted(supported_scopes),
                "verified": False,
            }
        if not str(request_id or "").strip() or not value:
            raise ValueError("reflection subject deletion requires request_id and scope_value")

        normalized_value = value.lower() if kind in {"agent", "project"} else value
        scope_hash = _deletion_scope_hash(kind, normalized_value)
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            prior = conn.execute(
                _reflection_deletion_sql(
                    """
                SELECT COUNT(*)
                FROM {reflection_deletion_table}
                WHERE scope_kind=? AND scope_value_hash=? AND status='applied'
                """
                ),
                (kind, scope_hash),
            ).fetchone()
            record_headers = conn.execute(
                "SELECT id, access_control FROM reflection_records"
            ).fetchall()
            selected_records: list[tuple[str, str]] = []
            unresolved_legacy_records = 0
            for header in record_headers:
                raw_access = header["access_control"]
                try:
                    access = validate_cognitive_access_envelope(json.loads(str(raw_access or "")))
                    if access["scope"]["resolution"] != "resolved":
                        raise ValueError("reflection ACL scope is unresolved")
                    before_acl_hash = cognitive_access_hash(access)
                except (TypeError, ValueError, json.JSONDecodeError):
                    unresolved_legacy_records += 1
                    if kind != "all":
                        continue
                    before_acl_hash = "sha256:" + hashlib.sha256(
                        str(raw_access or "").encode("utf-8")
                    ).hexdigest()
                if kind == "all" or cognitive_access_matches_subject(
                    access,
                    scope_kind=kind,
                    scope_value=normalized_value,
                ):
                    selected_records.append((str(header["id"]), before_acl_hash))

            selected_record_ids = {record_id for record_id, _hash in selected_records}
            shift_headers = conn.execute(
                "SELECT id, related_reflection_id, access_control FROM cognitive_shifts"
            ).fetchall()
            selected_shifts: list[tuple[int, str]] = []
            unresolved_legacy_shifts = 0
            for header in shift_headers:
                raw_access = header["access_control"]
                linked_record = str(header["related_reflection_id"] or "")
                try:
                    access = validate_cognitive_access_envelope(json.loads(str(raw_access or "")))
                    if access["scope"]["resolution"] != "resolved":
                        raise ValueError("reflection shift ACL scope is unresolved")
                    before_acl_hash = cognitive_access_hash(access)
                    matches = cognitive_access_matches_subject(
                        access,
                        scope_kind=kind,
                        scope_value=normalized_value,
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    unresolved_legacy_shifts += 1
                    before_acl_hash = "sha256:" + hashlib.sha256(
                        str(raw_access or "").encode("utf-8")
                    ).hexdigest()
                    matches = False
                if kind == "all" or linked_record in selected_record_ids or matches:
                    selected_shifts.append((int(header["id"]), before_acl_hash))

            layer5_headers = conn.execute(
                "SELECT id, access_control FROM layer5_experiences"
            ).fetchall()
            selected_layer5: list[tuple[int, str]] = []
            unresolved_legacy_layer5 = 0
            for header in layer5_headers:
                raw_access = header["access_control"]
                try:
                    access = validate_cognitive_access_envelope(
                        json.loads(str(raw_access or ""))
                    )
                    if access["scope"]["resolution"] != "resolved":
                        raise ValueError("Layer-5 ACL scope is unresolved")
                    before_acl_hash = cognitive_access_hash(access)
                    matches = cognitive_access_matches_subject(
                        access,
                        scope_kind=kind,
                        scope_value=normalized_value,
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    unresolved_legacy_layer5 += 1
                    before_acl_hash = "sha256:" + hashlib.sha256(
                        str(raw_access or "").encode("utf-8")
                    ).hexdigest()
                    matches = False
                if kind == "all" or matches:
                    selected_layer5.append((int(header["id"]), before_acl_hash))

            target_count = (
                len(selected_records) + len(selected_shifts) + len(selected_layer5)
            )
            if not target_count:
                prior_count = int(prior[0] or 0) if prior is not None else 0
                return {
                    "status": "existing" if prior_count else "no_targets",
                    "target_count": prior_count,
                    "receipt_count": prior_count,
                    "legacy_unscoped_layer5_count": unresolved_legacy_layer5,
                    "unresolved_legacy_record_count": unresolved_legacy_records,
                    "unresolved_legacy_shift_count": unresolved_legacy_shifts,
                    "verified": (
                        unresolved_legacy_layer5 == 0
                        and unresolved_legacy_records == 0
                        and unresolved_legacy_shifts == 0
                    ),
                }

            now = datetime.now().isoformat()
            receipt_count = 0
            try:
                for shift_id, before_acl_hash in selected_shifts:
                    object_type = "cognitive_shift"
                    object_id = str(shift_id)
                    conn.execute(
                        _reflection_deletion_sql(
                            """
                        INSERT INTO {reflection_deletion_table} (
                            receipt_id, schema_version, request_id, scope_kind,
                            scope_value_hash, object_type, object_id, before_acl_hash,
                            status, created_at, applied_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)
                        """
                        ),
                        (
                            _reflection_deletion_receipt_id(
                                request_id=str(request_id),
                                object_type=object_type,
                                object_id=object_id,
                                scope_hash=scope_hash,
                            ),
                            REFLECTION_DELETION_SCHEMA_VERSION,
                            str(request_id),
                            kind,
                            scope_hash,
                            object_type,
                            object_id,
                            before_acl_hash,
                            now,
                            now,
                        ),
                    )
                    conn.execute("DELETE FROM cognitive_shifts WHERE id=?", (shift_id,))
                    receipt_count += 1

                for record_id, before_acl_hash in selected_records:
                    object_type = "reflection_record"
                    conn.execute(
                        _reflection_deletion_sql(
                            """
                        INSERT INTO {reflection_deletion_table} (
                            receipt_id, schema_version, request_id, scope_kind,
                            scope_value_hash, object_type, object_id, before_acl_hash,
                            status, created_at, applied_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)
                        """
                        ),
                        (
                            _reflection_deletion_receipt_id(
                                request_id=str(request_id),
                                object_type=object_type,
                                object_id=record_id,
                                scope_hash=scope_hash,
                            ),
                            REFLECTION_DELETION_SCHEMA_VERSION,
                            str(request_id),
                            kind,
                            scope_hash,
                            object_type,
                            record_id,
                            before_acl_hash,
                            now,
                            now,
                        ),
                    )
                    conn.execute("DELETE FROM reflection_records WHERE id=?", (record_id,))
                    receipt_count += 1

                for layer5_id, before_acl_hash in selected_layer5:
                    object_type = "layer5_experience"
                    object_id = str(layer5_id)
                    conn.execute(
                        _reflection_deletion_sql(
                            """
                        INSERT INTO {reflection_deletion_table} (
                            receipt_id, schema_version, request_id, scope_kind,
                            scope_value_hash, object_type, object_id, before_acl_hash,
                            status, created_at, applied_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)
                        """
                        ),
                        (
                            _reflection_deletion_receipt_id(
                                request_id=str(request_id),
                                object_type=object_type,
                                object_id=object_id,
                                scope_hash=scope_hash,
                            ),
                            REFLECTION_DELETION_SCHEMA_VERSION,
                            str(request_id),
                            kind,
                            scope_hash,
                            object_type,
                            object_id,
                            before_acl_hash,
                            now,
                            now,
                        ),
                    )
                    conn.execute("DELETE FROM layer5_experiences WHERE id=?", (layer5_id,))
                    receipt_count += 1
            except sqlite3.Error:
                conn.rollback()
                return {
                    "status": "blocked",
                    "target_count": 0,
                    "receipt_count": 0,
                    "error": "reflection_subject_deletion_failed",
                    "verified": False,
                }

            remaining_layer5_scope = 0
            remaining_legacy_layer5 = 0
            for row in conn.execute(
                "SELECT access_control FROM layer5_experiences"
            ).fetchall():
                try:
                    access = validate_cognitive_access_envelope(
                        json.loads(str(row[0] or ""))
                    )
                    if access["scope"]["resolution"] != "resolved":
                        raise ValueError("Layer-5 ACL scope is unresolved")
                    if kind == "all" or cognitive_access_matches_subject(
                        access,
                        scope_kind=kind,
                        scope_value=normalized_value,
                    ):
                        remaining_layer5_scope += 1
                except (TypeError, ValueError, json.JSONDecodeError):
                    remaining_legacy_layer5 += 1
            return {
                "status": "applied",
                "target_count": target_count,
                "receipt_count": receipt_count,
                "reflection_records_deleted": len(selected_records),
                "cognitive_shifts_deleted": len(selected_shifts),
                "layer5_experiences_deleted": len(selected_layer5),
                "legacy_layer5_deleted": (
                    unresolved_legacy_layer5 if kind == "all" else 0
                ),
                "legacy_unscoped_layer5_count": remaining_legacy_layer5,
                "layer5_scope_residual_count": remaining_layer5_scope,
                "unresolved_legacy_record_count": unresolved_legacy_records,
                "unresolved_legacy_shift_count": unresolved_legacy_shifts,
                "verified": (
                    remaining_layer5_scope == 0
                    and remaining_legacy_layer5 == 0
                    and unresolved_legacy_records == 0
                    and unresolved_legacy_shifts == 0
                ),
            }

    def cleanup_older_than(self, days: int, dry_run: bool = False) -> int:
        """清理/统计反射记录、认知变迁和 Layer5 经验中的过期数据。"""
        if not dry_run:
            self._assert_writable()
        with self._read_connection() as conn:
            total = 0
            total += delete_older_than(conn, "reflection_records", "created_at", days, dry_run=dry_run)
            total += delete_older_than(conn, "cognitive_shifts", "shift_detected_at", days, dry_run=dry_run)
            total += delete_older_than(conn, "layer5_experiences", "timestamp", days, dry_run=dry_run)
            return total

    def get_stats(self) -> Dict:
        """获取存储统计"""
        with self._read_connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM reflection_records").fetchone()[0]
            total_shifts = conn.execute("SELECT COUNT(*) FROM cognitive_shifts").fetchone()[0]
            by_trigger = conn.execute(
                "SELECT trigger, COUNT(*) FROM reflection_records GROUP BY trigger"
            ).fetchall()
            with_validation = conn.execute(
                "SELECT COUNT(*) FROM reflection_records WHERE internal_validation != '{}'"
            ).fetchone()[0]
            latest = conn.execute("SELECT MAX(created_at) FROM reflection_records").fetchone()[0]

        return {
            "total_reflections": total,
            "total_shifts": total_shifts,
            "by_trigger": {t: c for t, c in by_trigger},
            "by_feedback": {},
            "by_implicit_feedback": {},
            "feedback_status": "legacy_feedback_quarantined",
            "with_internal_validation": with_validation,
            "latest_reflection": latest,
        }

    def _row_to_record(self, row: sqlite3.Row) -> ReflectionRecord:
        """将数据库行转换为 ReflectionRecord"""
        # Parse mirror snapshots
        mirror_snapshots = []
        try:
            snapshots_data = json.loads(row["mirror_snapshots"])
            for s in snapshots_data:
                mirror_snapshots.append(
                    MirrorSnapshot(
                        observation_id=s.get("observation_id", ""),
                        dimension=s.get("dimension", ""),
                        value_summary=s.get("value_summary", ""),
                        evidence_summary=s.get("evidence_summary", ""),
                        confidence=s.get("confidence", 1.0),
                        recency_weight=s.get("recency_weight", 1.0),
                        period_end=(
                            datetime.fromisoformat(s["period_end"]) if s.get("period_end") else None
                        ),
                    )
                )
        except (json.JSONDecodeError, KeyError):
            logging.getLogger(__name__).warning(
                "[reflection_store] (json.JSONDecodeError, KeyError) suppressed", exc_info=True
            )

        # Parse insight
        insight = None
        try:
            if row["insight_summary"]:
                insight = InsightSnapshot(
                    summary=row["insight_summary"],
                    key_points=json.loads(row["insight_key_points"]),
                    dimensions_involved=json.loads(row["insight_dimensions"]),
                )
        except (json.JSONDecodeError, KeyError):
            logging.getLogger(__name__).warning(
                "[reflection_store] (json.JSONDecodeError, KeyError) suppressed", exc_info=True
            )

        # Parse feedback
        feedback = None
        if row["feedback_type"]:
            feedback = UserFeedback(
                feedback_type=FeedbackType(row["feedback_type"]),
                comment=row["feedback_comment"] or "",
                given_at=(
                    datetime.fromisoformat(row["feedback_given_at"])
                    if row["feedback_given_at"]
                    else datetime.now()
                ),
            )

        # Parse implicit feedback
        implicit_feedback = None
        if row["implicit_feedback_type"]:
            try:
                implicit_feedback = ImplicitFeedbackRecord(
                    inferred_type=FeedbackType(row["implicit_feedback_type"]),
                    confidence=row["implicit_feedback_confidence"] or 0.0,
                    signals=(
                        json.loads(row["implicit_feedback_signals"])
                        if row["implicit_feedback_signals"]
                        else []
                    ),
                    inferred_at=(
                        datetime.fromisoformat(row["implicit_feedback_at"])
                        if row["implicit_feedback_at"]
                        else datetime.now()
                    ),
                )
            except (ValueError, TypeError):
                logging.getLogger(__name__).warning(
                    "[reflection_store] (ValueError, TypeError) suppressed", exc_info=True
                )

        # Parse internal validation
        internal_validation = None
        if row["internal_validation"]:
            try:
                internal_validation = json.loads(row["internal_validation"])
            except json.JSONDecodeError:
                logging.getLogger(__name__).warning(
                    "[reflection_store] json.JSONDecodeError suppressed", exc_info=True
                )

        return ReflectionRecord(
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            trigger=ReflectionTrigger(row["trigger"]),
            trigger_event=row["trigger_event"] or "",
            user_query=row["user_query"] or "",
            mirror_snapshots=mirror_snapshots,
            mirror_dimensions=(
                json.loads(row["mirror_dimensions"]) if row["mirror_dimensions"] else []
            ),
            insight=insight,
            temporal_context=(
                json.loads(row["temporal_context"]) if row["temporal_context"] else None
            ),
            user_feedback=feedback,
            implicit_feedback=implicit_feedback,
            internal_validation=internal_validation,
            fed_back_to_observations=bool(row["fed_back_to_observations"]),
            fed_back_to_knowledge=bool(row["fed_back_to_knowledge"]),
            access_control=_load_reflection_access(
                row["access_control"] if "access_control" in row.keys() else "",
                object_ref=str(row["id"]),
            ),
        )

    def _row_to_shift(self, row: sqlite3.Row) -> CognitiveShift:
        """将数据库行转换为 CognitiveShift"""
        return CognitiveShift(
            dimension=row["dimension"],
            shift_type=row["shift_type"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            confidence=row["confidence"] or 0.0,
            evidence=json.loads(row["evidence"]) if row["evidence"] else [],
            first_seen_at=datetime.fromisoformat(row["first_seen_at"])
            if row["first_seen_at"]
            else None,
            shift_detected_at=datetime.fromisoformat(row["shift_detected_at"]),
            access_control=_load_reflection_access(
                row["access_control"] if "access_control" in row.keys() else "",
                object_ref=f"shift:{row['id']}",
            ),
            related_reflection_id=str(row["related_reflection_id"] or ""),
        )
