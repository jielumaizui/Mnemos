"""
Observation Store — SQLite 持久化层

支持：
- 增量更新（按 source_id + dimension + observation_type 去重）
- 按维度、时间窗口、source_type 查询
- 版本追踪（version 字段）
"""

import json
import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_access,
    cognitive_access_hash,
    cognitive_access_matches_subject,
    make_cognitive_access_envelope,
    validate_cognitive_access_envelope,
)
from core.cognitive.models import Dimension, Observation, SourceType
from core.cognitive.calibration_math import canonical_hash
from core.cognitive.observation_calibration_schema import (
    initialize_observation_calibration_schema,
    validate_observation_calibration_schema,
)
from core.db_utils import delete_older_than, render_sql
from core.privacy.content_redaction import redact_persistence_value

logger = logging.getLogger(__name__)

OBSERVATION_DELETION_SCHEMA_VERSION = "mnemos.observation_subject_deletion.v1"
OBSERVATION_DELETION_TABLE = "observation_subject_deletion_receipts"


def _observation_deletion_scope_hash(scope_kind: str, scope_value: str) -> str:
    material = f"{scope_kind}\x1f{scope_value}".encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _observation_deletion_receipt_id(
    *, request_id: str, observation_id: str, scope_hash: str
) -> str:
    material = "\x1f".join((request_id, observation_id, scope_hash)).encode("utf-8")
    return "observation-delete-" + hashlib.sha256(material).hexdigest()[:40]


class ObservationCalibrationMigrationRequired(RuntimeError):
    """Raised when an existing Observation DB lacks calibration bindings."""


# 防止单次提取输入无限增长：限制 item 数量和单 item 内容长度
MAX_ITEMS_PER_EXTRACTION = 1000
MAX_CONTENT_CHARS_PER_ITEM = 50000

_OBSERVATION_READ_PURPOSES = (
    "observation_read",
    "preflight_inject",
    # Canonical Raw-derived observations may participate in a reflection only
    # through MirrorEngine's authorized retrieval seam.  Listing the exact
    # downstream purposes here lets a derived ReflectionRecord prove consent
    # rather than inventing a broader permission later.
    "reflection_read",
    "reflection_feedback",
    "reflection_prompt",
    "reflection_experience_read",
    "reflection_export",
)


def _ensure_observation_access_control(obs: Observation) -> dict[str, Any]:
    """Return a complete object ACL, converting unknown historical input to deny-all.

    Observation producers historically did not carry an ACL.  Persisting a
    restricted-unknown envelope keeps those measurements auditable while
    making them unreadable through every prompt-facing retrieval seam.
    """

    if obs.access_control:
        access_control = validate_cognitive_access_envelope(
            obs.access_control,
            expected_scope_type="observation",
            expected_scope_id=obs.id,
        )
    else:
        access_control = make_cognitive_access_envelope(
            owner_principal_id="system:observation-store",
            owner_agent="system",
            scope_type="observation",
            scope_id=obs.id,
            purposes=_OBSERVATION_READ_PURPOSES,
            consent_provenance_refs=(),
            sensitivity="restricted",
            retention_policy="observation_retention",
            source_acl_lineage=(
                "observation-source:"
                + str(obs.source_type.value)
                + ":"
                + str(obs.source_id or obs.id),
            ),
            visibility="restricted",
            scope_resolution="restricted_unknown",
            consent_status="restricted_unknown",
        )
    obs.access_control = dict(access_control)
    return access_control


def _truncate_items(items: List) -> List:
    """限制提取输入规模，防止大 backlog 时 OOM/慢正则。"""
    if not items:
        return items
    original_count = len(items)

    def _ts_key(item):
        ts = item.timestamp
        if ts is None:
            return 0.0
        # 统一转为 naive UTC 时间戳，避免 aware/naive 比较错误
        try:
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except (AttributeError, TypeError, ValueError):
            # 非预期时间对象时保留原值，避免单条数据影响排序
            pass
        return ts.timestamp()

    # 按时间由新到旧排序，保留最近的 N 个
    sorted_items = sorted(items, key=_ts_key, reverse=True)
    truncated = sorted_items[:MAX_ITEMS_PER_EXTRACTION]
    # Canonical Raw is an immutable evidence contract.  It must never be
    # silently shortened before an extractor gets a chance to classify it;
    # callers process canonical revisions one at a time.  Wiki and diagnostic
    # inputs retain the defensive bound below.
    for item in truncated:
        if (
            hasattr(item, "content")
            and not getattr(item, "raw_revision_id", "")
            and len(item.content) > MAX_CONTENT_CHARS_PER_ITEM
        ):
            item.content = item.content[:MAX_CONTENT_CHARS_PER_ITEM] + "\n\n[... 内容已截断 ...]"
    if original_count > MAX_ITEMS_PER_EXTRACTION:
        logger.warning(
            "[Observation] 输入 item 过多: %d, 仅处理最近 %d 个",
            original_count,
            MAX_ITEMS_PER_EXTRACTION,
        )
    return truncated


def _extract_observations(items, extractors=None, *, fail_fast: bool = False) -> List[Observation]:
    """从 SourceItem 列表中提取 Observation，统一处理聚合型来源。

    该函数是 ObservationEngine._run_extraction 与
    ObservationIndex.rebuild_from_sources 的公共提取逻辑，避免重复。
    """
    from core.cognitive.dimension_extractors import ALL_EXTRACTORS

    if extractors is None:
        extractors = ALL_EXTRACTORS

    items = _truncate_items(items)
    observations: List[Observation] = []
    for extractor in extractors:
        try:
            for obs in extractor.extract(items):
                # 聚合型 Observation 标记为汇总来源
                if not obs.source_path:
                    wiki_items = [i for i in items if i.source_type == "wiki"]
                    raw_items = [i for i in items if i.source_type == "raw"]
                    parts = []
                    if wiki_items:
                        parts.append(f"wiki:{len(wiki_items)}")
                    if raw_items:
                        parts.append(f"raw:{len(raw_items)}")
                    obs.source_path = f"aggregated:{','.join(parts)}"
                if not obs.source_id:
                    if obs.source_path.startswith("aggregated:"):
                        obs.source_id = "aggregate"
                    elif obs.source_path.startswith("system:"):
                        obs.source_id = "system"
                    else:
                        obs.source_id = "unknown"
                observations.append(obs)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RuntimeError,
        ) as e:
            if fail_fast:
                raise
            import logging

            logging.getLogger(__name__).error(
                "Extractor %s failed: %s", extractor.dimension.value, e
            )
    return observations


class ObservationStore:
    """Observation SQLite 存储"""

    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        ownership_config: Any | None = None,
        initialize: bool = True,
        read_only: bool = False,
    ):
        """
        初始化存储

        Args:
            db_path: 数据库文件路径，默认 ~/.mnemos/observations.db
        """
        if db_path:
            self.db_path = Path(db_path)
        else:
            self.db_path = Path.home() / ".mnemos" / "observations.db"
        if ownership_config is None:
            from core.config import get_config

            ownership_config = get_config()
        self._ownership_config = ownership_config
        self.read_only = bool(read_only)
        if initialize:
            if self.read_only:
                raise ValueError("read-only ObservationStore cannot initialize schema")
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
            raise PermissionError("read-only ObservationStore cannot mutate canonical state")

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
                "observation write is blocked by a matching frozen data ownership scope"
            )

    def _init_db(self):
        """初始化数据库表结构"""
        with self._read_connection() as conn:
            observations_existed = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
                ).fetchone()
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY,
                    dimension TEXT NOT NULL,
                    observation_type TEXT NOT NULL,
                    value TEXT NOT NULL,           -- JSON
                    unit TEXT DEFAULT '',
                    confidence REAL DEFAULT 1.0,
                    source_type TEXT NOT NULL,
                    source_path TEXT DEFAULT '',
                    source_id TEXT DEFAULT '',
                    evidence TEXT DEFAULT '[]',     -- JSON list
                    access_control TEXT NOT NULL DEFAULT '', -- canonical object ACL JSON
                    observed_at TEXT,
                    period_start TEXT,
                    period_end TEXT,
                    content_source TEXT DEFAULT 'unknown',
                    user_intent_signal TEXT DEFAULT 'unknown',
                    user_notes TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER DEFAULT 1
                )
            """)
            if not observations_existed:
                initialize_observation_calibration_schema(conn)
            try:
                validate_observation_calibration_schema(conn)
            except RuntimeError as exc:
                raise ObservationCalibrationMigrationRequired(
                    str(exc)
                ) from exc
            # 索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_obs_dimension
                ON observations(dimension)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_obs_source
                ON observations(source_type, source_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_obs_period
                ON observations(period_start, period_end)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_obs_updated
                ON observations(updated_at)
            """)
            # This is consumer progress, not a second cognition ledger.  A
            # cursor is advanced only after this Observation consumer has
            # persisted an exact Raw provenance edge or a typed terminal
            # no-observation receipt.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS observation_source_cursors (
                    source_stream TEXT PRIMARY KEY,
                    cursor_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {OBSERVATION_DELETION_TABLE} (
                    receipt_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    scope_kind TEXT NOT NULL,
                    scope_value_hash TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    before_acl_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status='applied'),
                    created_at TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    UNIQUE(request_id, observation_id, scope_value_hash)
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_observation_subject_deletion_scope
                ON {OBSERVATION_DELETION_TABLE}(
                    scope_kind, scope_value_hash, status
                )
                """
            )
            conn.commit()

    def get_source_cursors(self) -> Dict[str, Dict[str, str]]:
        """Return durable per-source progress tokens owned by Observation."""
        with self._read_connection() as conn:
            rows = conn.execute(
                "SELECT source_stream, cursor_json FROM observation_source_cursors"
            ).fetchall()
        cursors: Dict[str, Dict[str, str]] = {}
        for source_stream, raw_token in rows:
            try:
                token = json.loads(raw_token)
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.warning("Invalid Observation source cursor for %s", source_stream)
                continue
            if isinstance(token, dict) and all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in token.items()
            ):
                cursors[str(source_stream)] = dict(token)
        return cursors

    def set_source_cursor(self, source_stream: str, token: Mapping[str, str]) -> None:
        """Persist one source cursor after its terminal work is durable."""
        self._assert_writable()
        if not source_stream or not token:
            raise ValueError("Observation source cursor requires a stream and token")
        normalized = {str(key): str(value) for key, value in token.items()}
        with self._read_connection() as conn:
            conn.execute(
                """
                INSERT INTO observation_source_cursors (source_stream, cursor_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_stream) DO UPDATE SET
                    cursor_json=excluded.cursor_json,
                    updated_at=excluded.updated_at
                """,
                (
                    source_stream,
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()

    def save(self, obs: Observation) -> str:
        """
        保存单个 Observation（支持增量更新）

        去重策略：同一 source_id + dimension + observation_type 视为同一观察，
        更新 version 和 updated_at。

        Returns:
            "inserted": 新插入
            "updated":  已有记录且内容发生变化，已更新
            "unchanged": 已有记录且内容未发生变化
        """

        self._assert_writable()
        pointer_fields = (
            obs.calibration_revision_id,
            obs.calibration_input_hash,
            obs.calibration_spec_hash,
            obs.calibration_record_hash,
        )
        if any(pointer_fields):
            raise ValueError(
                "ObservationStore.save accepts base measurements only; "
                "use CalibrationRecordStore.apply_to_observation after commit"
            )

        initial_access = _ensure_observation_access_control(obs)
        self._assert_write_not_frozen(
            initial_access,
            source_event_ids=tuple(
                value for value in (str(obs.source_id or ""),) if value
            ),
        )

        # An unbound confidence supplied by an Observation producer is a new
        # base measurement.  A posterior is recognizable only by its complete
        # committed calibration pointer.
        if not obs.calibration_revision_id and obs.base_measurement_status == "verified":
            obs.base_confidence = float(obs.confidence)
        # Capture exact pre-redaction input identity in memory.  The literal is
        # then discarded at the persistence boundary; only this digest enters
        # the committed CalibrationRecord snapshot.
        raw_measurement_payload = obs.calibration_measurement_payload()
        raw_peer_payload = obs.calibration_peer_payload()

        def _bind_pre_redaction_hashes() -> None:
            raw_measurement_payload["observation_id"] = obs.id
            obs.calibration_measurement_hash = canonical_hash(raw_measurement_payload)
            obs.calibration_peer_hash = canonical_hash(raw_peer_payload)

        _bind_pre_redaction_hashes()

        redacted = redact_persistence_value(
            {
                "value": obs.value,
                "evidence": obs.evidence,
                "source_path": obs.source_path,
                "user_notes": obs.user_notes,
            }
        ).value
        if not isinstance(redacted, Mapping):
            raise ValueError("redacted Observation payload must remain an object")
        obs.value = redacted["value"]
        obs.evidence = list(redacted["evidence"])
        obs.source_path = str(redacted["source_path"])
        obs.user_notes = str(redacted["user_notes"])

        def _equal(row) -> bool:
            """比较现有行与待保存 Observation 的内容是否一致"""
            if row["dimension"] != obs.dimension.value:
                return False
            if row["observation_type"] != obs.observation_type.value:
                return False
            if json.loads(row["value"]) != obs.value:
                return False
            if (row["unit"] or "") != (obs.unit or ""):
                return False
            if row["confidence"] != obs.confidence:
                return False
            if row["base_confidence"] != obs.base_confidence:
                return False
            if row["base_measurement_status"] != obs.base_measurement_status:
                return False
            for field_name in (
                "calibration_revision_id",
                "calibration_input_hash",
                "calibration_spec_hash",
                "calibration_record_hash",
            ):
                if (row[field_name] or "") != getattr(obs, field_name):
                    return False
            if row["source_type"] != obs.source_type.value:
                return False
            if row["source_path"] != obs.source_path:
                return False
            if row["source_id"] != obs.source_id:
                return False
            if json.loads(row["evidence"] or "[]") != (obs.evidence or []):
                return False
            if json.loads(row["source_span_ids"] or "[]") != (obs.source_span_ids or []):
                return False
            if str(row["access_control"] or "") != access_control_json:
                return False
            if row["observed_at"] != (obs.observed_at.isoformat() if obs.observed_at else None):
                return False
            if row["period_start"] != (obs.period_start.isoformat() if obs.period_start else None):
                return False
            if row["period_end"] != (obs.period_end.isoformat() if obs.period_end else None):
                return False
            if row["content_source"] != obs.content_source.value:
                return False
            if row["user_intent_signal"] != obs.user_intent_signal.value:
                return False
            if (row["user_notes"] or "") != (obs.user_notes or ""):
                return False
            return True

        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            # 检查是否已存在
            cursor = conn.execute(
                """SELECT * FROM observations
                   WHERE source_id = ? AND dimension = ? AND observation_type = ?""",
                (obs.source_id, obs.dimension.value, obs.observation_type.value),
            )
            existing = cursor.fetchone()

            if existing is not None:
                # The database identity, not a transient extractor UUID, owns
                # the persisted object ACL.  Preserve every inherited source
                # constraint while rebinding the envelope to that stable row.
                obs.id = str(existing["id"])
                if obs.access_control:
                    rebound = dict(obs.access_control)
                    raw_scope = rebound.get("scope")
                    if isinstance(raw_scope, Mapping):
                        rebound["scope"] = {**dict(raw_scope), "scope_id": obs.id}
                    obs.access_control = rebound
            access_control = _ensure_observation_access_control(obs)
            access_control_json = json.dumps(
                access_control,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

            if existing:
                # Exact base-measurement replay retains a previously committed
                # calibration pointer.  Changed input first returns to the
                # immutable prior; a new CalibrationRecord is committed before
                # ``CalibrationRecordStore.apply_to_observation`` may bind
                # another posterior.
                base_equal = (
                    json.loads(existing["value"]) == obs.value
                    and (existing["unit"] or "") == (obs.unit or "")
                    and float(existing["base_confidence"])
                    == obs.base_confidence_value()
                    and existing["base_measurement_status"]
                    == obs.base_measurement_status
                    and existing["source_type"] == obs.source_type.value
                    and existing["source_path"] == obs.source_path
                    and existing["source_id"] == obs.source_id
                    and json.loads(existing["evidence"] or "[]") == (obs.evidence or [])
                    and json.loads(existing["source_span_ids"] or "[]")
                    == (obs.source_span_ids or [])
                    and existing["period_start"]
                    == (obs.period_start.isoformat() if obs.period_start else None)
                    and existing["period_end"]
                    == (obs.period_end.isoformat() if obs.period_end else None)
                    and existing["content_source"] == obs.content_source.value
                    and existing["user_intent_signal"]
                    == obs.user_intent_signal.value
                )
                if (
                    base_equal
                    and not obs.calibration_revision_id
                    and existing["calibration_revision_id"]
                ):
                    obs.confidence = float(existing["confidence"])
                    obs.calibration_revision_id = str(existing["calibration_revision_id"])
                    obs.calibration_input_hash = str(existing["calibration_input_hash"])
                    obs.calibration_spec_hash = str(existing["calibration_spec_hash"])
                    obs.calibration_record_hash = str(existing["calibration_record_hash"])
                elif not base_equal:
                    obs.confidence = obs.base_confidence_value()
                    obs.calibration_revision_id = ""
                    obs.calibration_input_hash = ""
                    obs.calibration_spec_hash = ""
                    obs.calibration_record_hash = ""
                # 内容完全一致时跳过更新，避免触发无意义的 Wiki 重导
                if _equal(existing):
                    obs.id = existing["id"]
                    obs.version = existing["version"]
                    _bind_pre_redaction_hashes()
                    return "unchanged"

                # 更新
                obs_id = existing["id"]
                version = existing["version"]
                try:
                    version = int(version)
                except (ValueError, TypeError):
                    version = 1
                # 同步内存对象的 id / version，确保证据链使用稳定 ID
                obs.id = obs_id
                obs.version = version + 1
                _bind_pre_redaction_hashes()
                conn.execute(
                    """UPDATE observations SET
                        value = ?, unit = ?, confidence = ?, base_confidence = ?,
                        base_measurement_status = ?,
                        calibration_revision_id = ?, calibration_input_hash = ?,
                        calibration_spec_hash = ?, calibration_record_hash = ?,
                        evidence = ?, source_span_ids = ?, observed_at = ?, period_start = ?, period_end = ?,
                        access_control = ?,
                        content_source = ?, user_intent_signal = ?, user_notes = ?,
                        updated_at = ?, version = ?
                       WHERE id = ?""",
                    (
                        json.dumps(obs.value, ensure_ascii=False),
                        obs.unit,
                        obs.confidence,
                        obs.base_confidence,
                        obs.base_measurement_status,
                        obs.calibration_revision_id,
                        obs.calibration_input_hash,
                        obs.calibration_spec_hash,
                        obs.calibration_record_hash,
                        json.dumps(obs.evidence, ensure_ascii=False),
                        json.dumps(obs.source_span_ids, ensure_ascii=False),
                        obs.observed_at.isoformat() if obs.observed_at else None,
                        obs.period_start.isoformat() if obs.period_start else None,
                        obs.period_end.isoformat() if obs.period_end else None,
                        access_control_json,
                        obs.content_source.value,
                        obs.user_intent_signal.value,
                        obs.user_notes,
                        datetime.now().isoformat(),
                        obs.version,
                        obs_id,
                    ),
                )
                conn.commit()
                return "updated"
            else:
                # 插入新记录
                conn.execute(
                    """INSERT INTO observations (
                        id, dimension, observation_type, value, unit, confidence,
                        base_confidence, base_measurement_status,
                        calibration_revision_id,
                        calibration_input_hash, calibration_spec_hash,
                        calibration_record_hash, source_type, source_path,
                        source_id, evidence, source_span_ids, access_control, observed_at,
                        period_start, period_end, content_source,
                        user_intent_signal, user_notes, created_at, updated_at,
                        version
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )""",
                    (
                        obs.id,
                        obs.dimension.value,
                        obs.observation_type.value,
                        json.dumps(obs.value, ensure_ascii=False),
                        obs.unit,
                        obs.confidence,
                        obs.base_confidence,
                        obs.base_measurement_status,
                        obs.calibration_revision_id,
                        obs.calibration_input_hash,
                        obs.calibration_spec_hash,
                        obs.calibration_record_hash,
                        obs.source_type.value,
                        obs.source_path,
                        obs.source_id,
                        json.dumps(obs.evidence, ensure_ascii=False),
                        json.dumps(obs.source_span_ids, ensure_ascii=False),
                        access_control_json,
                        obs.observed_at.isoformat() if obs.observed_at else None,
                        obs.period_start.isoformat() if obs.period_start else None,
                        obs.period_end.isoformat() if obs.period_end else None,
                        obs.content_source.value,
                        obs.user_intent_signal.value,
                        obs.user_notes,
                        obs.created_at.isoformat(),
                        obs.updated_at.isoformat(),
                        obs.version,
                    ),
                )
                conn.commit()
                return "inserted"

    def _apply_committed_calibration(
        self,
        observation_id: str,
        *,
        prior: float,
        posterior: float,
        revision_id: str,
        input_hash: str,
        spec_hash: str,
        record_hash: str,
    ) -> Dict[str, Any]:
        """Bind a posterior only after its canonical revision is committed."""

        self._assert_writable()
        from core.cognitive.state_contract import sha256_json

        identity = {
            "observation_id": str(observation_id or ""),
            "base_confidence": float(prior),
        }
        pointer = {
            "calibration_revision_id": str(revision_id or ""),
            "calibration_input_hash": str(input_hash or ""),
            "calibration_spec_hash": str(spec_hash or ""),
            "calibration_record_hash": str(record_hash or ""),
            "confidence": float(posterior),
        }
        if not identity["observation_id"] or not all(pointer.values()):
            raise ValueError("complete calibration binding is required")
        if not 0.0 <= float(prior) <= 1.0 or not 0.0 <= float(posterior) <= 1.0:
            raise ValueError("calibration confidence is outside [0, 1]")
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM observations WHERE id=?",
                (identity["observation_id"],),
            ).fetchone()
            if row is None:
                raise ValueError("calibration target observation does not exist")
            if str(row["base_measurement_status"]) != "verified":
                raise ValueError(
                    "calibration requires a verified base measurement; re-extract the Observation"
                )
            if abs(float(row["base_confidence"]) - float(prior)) > 1e-9:
                raise ValueError("calibration prior does not match the base measurement")
            before = {
                "observation_id": identity["observation_id"],
                "base_confidence": float(row["base_confidence"]),
                "base_measurement_status": str(row["base_measurement_status"]),
                "calibration_revision_id": str(row["calibration_revision_id"] or ""),
                "calibration_input_hash": str(row["calibration_input_hash"] or ""),
                "calibration_spec_hash": str(row["calibration_spec_hash"] or ""),
                "calibration_record_hash": str(row["calibration_record_hash"] or ""),
                "confidence": float(row["confidence"]),
            }
            after = {
                **identity,
                "base_measurement_status": "verified",
                **pointer,
            }
            before_hash = sha256_json(before)
            after_hash = sha256_json(after)
            if before == after:
                return {
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                    "changed": False,
                }
            conn.execute(
                """
                UPDATE observations SET confidence=?, calibration_revision_id=?,
                    calibration_input_hash=?, calibration_spec_hash=?,
                    calibration_record_hash=?, updated_at=?
                WHERE id=?
                """,
                (
                    float(posterior),
                    str(revision_id),
                    str(input_hash),
                    str(spec_hash),
                    str(record_hash),
                    datetime.now().isoformat(),
                    identity["observation_id"],
                ),
            )
            conn.commit()
        return {
            "before_hash": before_hash,
            "after_hash": after_hash,
            "changed": before != after,
        }

    def save_batch(self, observations: List[Observation]) -> Dict[str, Any]:
        """批量保存，返回统计及发生变更的维度集合

        同一批内按 (source_id, dimension, observation_type) 去重并保留最后一条，
        避免中间状态互相覆盖导致无意义的 Wiki 重导。
        """
        self._assert_writable()
        from collections import OrderedDict

        deduped: OrderedDict[tuple, Observation] = OrderedDict()
        for obs in observations:
            key = (obs.source_id, obs.dimension.value, obs.observation_type.value)
            deduped[key] = obs

        stats: Dict[str, Any] = {
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "changed_dimensions": set(),
            # The engine must attach Raw provenance only to Observation rows
            # that this batch actually persisted.  ``batch.observations`` may
            # contain earlier same-key candidates discarded by this local
            # dedupe pass, whose transient IDs do not exist in SQLite.
            "persisted_observation_ids": set(),
        }
        for obs in deduped.values():
            result = self.save(obs)
            stats["persisted_observation_ids"].add(obs.id)
            if result == "inserted":
                stats["inserted"] += 1
                stats["changed_dimensions"].add(obs.dimension.value)
            elif result == "updated":
                stats["updated"] += 1
                stats["changed_dimensions"].add(obs.dimension.value)
            else:
                stats["unchanged"] += 1
        return stats

    def query(
        self,
        dimension: Optional[Dimension] = None,
        source_type: Optional[SourceType] = None,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Observation]:
        """查询 Observation"""
        conditions, params = self._query_filters(
            dimension=dimension,
            source_type=source_type,
            period_start=period_start,
            period_end=period_end,
        )

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        sql = " ".join([
            "SELECT * FROM observations",
            "WHERE",
            where_clause,
            "ORDER BY updated_at DESC",
            "LIMIT ?",
        ])
        params.append(limit)  # type: ignore[arg-type]

        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row  # noqa
            rows = conn.execute(sql, params).fetchall()

        return [self._row_to_obs(row) for row in rows]

    def query_calibrated(
        self,
        dimension: Optional[Dimension] = None,
    ) -> List[Observation]:
        """Return every Observation bound to a canonical CalibrationRecord.

        Projection replay must never let the regular detail limit evict an
        older calibrated object, because doing so also loses its required Wiki
        consumer receipt.  This maintenance seam is intentionally unbounded
        and only selects rows with a complete calibration pointer.
        """

        if dimension is None:
            sql = """
                SELECT * FROM observations
                WHERE calibration_revision_id <> ''
                ORDER BY updated_at DESC
            """
            params: tuple[str, ...] = ()
        else:
            sql = """
                SELECT * FROM observations
                WHERE calibration_revision_id <> '' AND dimension = ?
                ORDER BY updated_at DESC
            """
            params = (dimension.value,)
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_obs(row) for row in rows]

    def query_all_for_projection(
        self,
        dimension: Optional[Dimension] = None,
    ) -> List[Observation]:
        """Return the complete canonical denominator for deterministic replay."""

        if dimension is None:
            sql = "SELECT * FROM observations ORDER BY updated_at DESC, id ASC"
            params: tuple[str, ...] = ()
        else:
            sql = (
                "SELECT * FROM observations WHERE dimension = ? "
                "ORDER BY updated_at DESC, id ASC"
            )
            params = (dimension.value,)
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_obs(row) for row in rows]

    @staticmethod
    def _query_filters(
        *,
        dimension: Optional[Dimension],
        source_type: Optional[SourceType],
        period_start: Optional[datetime],
        period_end: Optional[datetime],
    ) -> tuple[List[str], List[Any]]:
        """Return semantic-body-free SQL predicates for observation headers."""

        conditions: List[str] = []
        params: List[Any] = []
        if dimension:
            conditions.append("dimension = ?")
            params.append(dimension.value)
        if source_type:
            conditions.append("source_type = ?")
            params.append(source_type.value)
        if period_start:
            conditions.append("period_end >= ?")
            params.append(period_start.isoformat())
        if period_end:
            conditions.append("period_start <= ?")
            params.append(period_end.isoformat())
        return conditions, params

    def authorized_query(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
        dimension: Optional[Dimension] = None,
        source_type: Optional[SourceType] = None,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
        limit: int = 100,
    ) -> tuple[List[Observation], Dict[str, Any]]:
        """Authorize ACL headers before fetching an Observation payload.

        This is the sole externally-visible read seam.  Internal owner
        maintenance may use ``query``, but prompt and MCP surfaces must
        call this method or the ``ObservationIndex`` wrapper below.
        """

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
        conditions, params = self._query_filters(
            dimension=dimension,
            source_type=source_type,
            period_start=period_start,
            period_end=period_end,
        )
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        denied_by_reason: Dict[str, int] = {}
        authorized_ids: List[str] = []
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            candidates = conn.execute(
                " ".join(
                    [
                        "SELECT id, access_control FROM observations",
                        "WHERE",
                        where_clause,
                        "ORDER BY updated_at DESC",
                    ]
                ),
                params,
            ).fetchall()
            for candidate in candidates:
                try:
                    access_control = validate_cognitive_access_envelope(
                        json.loads(str(candidate["access_control"] or "")),
                        expected_scope_type="observation",
                        expected_scope_id=str(candidate["id"]),
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    reason = "acl_unknown"
                else:
                    reason = authorize_cognitive_access(
                        access_control,
                        principal=principal,
                        narrowing=narrowing,
                        purpose=purpose,
                    ).reason
                if reason == "authorized":
                    authorized_ids.append(str(candidate["id"]))
                    if len(authorized_ids) >= max(0, int(limit)):
                        break
                else:
                    denied_by_reason[reason] = denied_by_reason.get(reason, 0) + 1
            if not authorized_ids:
                return [], {
                    "candidate_count": len(candidates),
                    "authorized_count": 0,
                    "denied_by_reason": denied_by_reason,
                }
            rows_by_id = {
                str(row["id"]): row
                for row in conn.execute(
                    render_sql(
                        "SELECT * FROM observations WHERE id IN ({observation_ids})",
                        placeholder_counts={
                            "observation_ids": len(authorized_ids)
                        },
                    ),
                    tuple(authorized_ids),
                ).fetchall()
            }
        return (
            [self._row_to_obs(rows_by_id[obs_id]) for obs_id in authorized_ids if obs_id in rows_by_id],
            {
                "candidate_count": len(candidates),
                "authorized_count": len(authorized_ids),
                "denied_by_reason": denied_by_reason,
            },
        )

    def get_by_id(self, obs_id: str) -> Optional[Observation]:
        """按 ID 查询单个 Observation"""
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row  # noqa
            row = conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
            if row is None:
                return None
            return self._row_to_obs(row)

    def delete_subject_scope(
        self,
        *,
        request_id: str,
        scope_kind: str,
        scope_value: str,
    ) -> Dict[str, Any]:
        """Delete ACL-matched observations with body-free durable receipts."""

        self._assert_writable()
        kind = str(scope_kind or "").strip().lower()
        value = str(scope_value or "").strip()
        supported_scopes = {"all", "agent", "session", "project"}
        if kind not in supported_scopes or (kind == "all" and value != "all"):
            return {
                "status": "unsupported_scope",
                "target_count": 0,
                "verified": False,
                "supported_scopes": sorted(supported_scopes),
            }
        if not str(request_id or "").strip() or not value:
            raise ValueError(
                "observation subject deletion requires request_id and scope_value"
            )
        normalized_value = value.lower() if kind in {"agent", "project"} else value
        scope_hash = _observation_deletion_scope_hash(kind, normalized_value)
        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            headers = conn.execute(
                "SELECT id, access_control FROM observations"
            ).fetchall()
            selected: list[tuple[str, str]] = []
            unresolved_legacy_count = 0
            for header in headers:
                raw_access = header["access_control"]
                try:
                    access = validate_cognitive_access_envelope(
                        json.loads(str(raw_access or "")),
                        expected_scope_type="observation",
                        expected_scope_id=str(header["id"]),
                    )
                    if access["scope"]["resolution"] != "resolved":
                        raise ValueError("observation ACL scope is unresolved")
                    before_acl_hash = cognitive_access_hash(access)
                    matches = cognitive_access_matches_subject(
                        access,
                        scope_kind=kind,
                        scope_value=normalized_value,
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    unresolved_legacy_count += 1
                    matches = False
                    before_acl_hash = "sha256:" + hashlib.sha256(
                        str(raw_access or "").encode("utf-8")
                    ).hexdigest()
                if kind == "all" or matches:
                    selected.append((str(header["id"]), before_acl_hash))

            prior_count = int(
                conn.execute(
                    render_sql(
                        """
                    SELECT COUNT(*) FROM {table}
                    WHERE scope_kind=? AND scope_value_hash=? AND status='applied'
                    """,
                        identifiers={"table": OBSERVATION_DELETION_TABLE},
                    ),
                    (kind, scope_hash),
                ).fetchone()[0]
                or 0
            )
            if not selected:
                status = "existing" if prior_count else "no_targets"
                return {
                    "status": status,
                    "target_count": prior_count,
                    "receipt_count": prior_count,
                    "after_count": 0,
                    "unresolved_legacy_count": unresolved_legacy_count,
                    "verified": unresolved_legacy_count == 0,
                }

            now = datetime.now(timezone.utc).isoformat()
            try:
                for observation_id, before_acl_hash in selected:
                    conn.execute(
                        render_sql(
                            """
                        INSERT OR IGNORE INTO {table} (
                            receipt_id, schema_version, request_id, scope_kind,
                            scope_value_hash, observation_id, before_acl_hash,
                            status, created_at, applied_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)
                        """,
                            identifiers={"table": OBSERVATION_DELETION_TABLE},
                        ),
                        (
                            _observation_deletion_receipt_id(
                                request_id=str(request_id),
                                observation_id=observation_id,
                                scope_hash=scope_hash,
                            ),
                            OBSERVATION_DELETION_SCHEMA_VERSION,
                            str(request_id),
                            kind,
                            scope_hash,
                            observation_id,
                            before_acl_hash,
                            now,
                            now,
                        ),
                    )
                    conn.execute(
                        "DELETE FROM observations WHERE id=?", (observation_id,)
                    )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                return {
                    "status": "blocked",
                    "target_count": 0,
                    "receipt_count": 0,
                    "after_count": None,
                    "unresolved_legacy_count": unresolved_legacy_count,
                    "verified": False,
                    "error": "observation_subject_deletion_failed",
                }

            after_count = int(
                conn.execute(
                    render_sql(
                        "SELECT COUNT(*) FROM observations "
                        "WHERE id IN ({observation_ids})",
                        placeholder_counts={"observation_ids": len(selected)},
                    ),
                    tuple(observation_id for observation_id, _hash in selected),
                ).fetchone()[0]
                or 0
            )
            if kind == "all":
                unresolved_legacy_count = 0
            return {
                "status": "applied",
                "target_count": len(selected),
                "receipt_count": len(selected),
                "after_count": after_count,
                "unresolved_legacy_count": unresolved_legacy_count,
                "verified": after_count == 0 and unresolved_legacy_count == 0,
            }

    def cleanup_older_than(self, days: int, dry_run: bool = False) -> int:
        """清理/统计 updated_at 早于保留期限的观察记录。"""
        if not dry_run:
            self._assert_writable()
        with self._read_connection() as conn:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))
            ).strftime("%Y-%m-%dT%H:%M:%S")
            calibrated_candidates = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM observations
                    WHERE updated_at < ? AND calibration_revision_id != ''
                    """,
                    (cutoff,),
                ).fetchone()[0]
            )
            if calibrated_candidates:
                raise RuntimeError(
                    "Observation retention cannot delete calibrated rows without "
                    "a coordinated CalibrationRecord retirement"
                )
            return delete_older_than(conn, "observations", "updated_at", days, dry_run=dry_run)

    def get_stats(self) -> Dict:
        """获取存储统计"""
        with self._read_connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            by_dimension = conn.execute(
                "SELECT dimension, COUNT(*) FROM observations GROUP BY dimension"
            ).fetchall()
            by_source = conn.execute(
                "SELECT source_type, COUNT(*) FROM observations GROUP BY source_type"
            ).fetchall()
            latest = conn.execute("SELECT MAX(updated_at) FROM observations").fetchone()[0]
            calibrated = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE calibration_revision_id != ''"
            ).fetchone()[0]

        return {
            "total_observations": total,
            "by_dimension": {d: c for d, c in by_dimension},
            "by_source": {s: c for s, c in by_source},
            "latest_update": latest,
            "calibrated_observations": calibrated,
        }

    def clear_all(self):
        """清空所有观察（谨慎使用）"""
        self._assert_writable()
        with self._read_connection() as conn:
            calibrated = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM observations
                    WHERE calibration_revision_id != ''
                    """
                ).fetchone()[0]
            )
            if calibrated:
                raise RuntimeError(
                    "Observation clear cannot orphan committed CalibrationRecords; "
                    "use a coordinated state retirement workflow"
                )
            conn.execute("DELETE FROM observations")
            conn.commit()

    def _row_to_obs(self, row: sqlite3.Row) -> Observation:
        """将数据库行转换为 Observation"""
        return Observation.from_dict(
            {
                "id": row["id"],
                "dimension": row["dimension"],
                "observation_type": row["observation_type"],
                "value": json.loads(row["value"]),
                "unit": row["unit"],
                "confidence": row["confidence"],
                "base_confidence": row["base_confidence"],
                "base_measurement_status": row["base_measurement_status"],
                "calibration_revision_id": row["calibration_revision_id"],
                "calibration_input_hash": row["calibration_input_hash"],
                "calibration_spec_hash": row["calibration_spec_hash"],
                "calibration_record_hash": row["calibration_record_hash"],
                "source_type": row["source_type"],
                "source_path": row["source_path"],
                "source_id": row["source_id"],
                "evidence": row["evidence"],
                "source_span_ids": row["source_span_ids"],
                "access_control": (
                    json.loads(row["access_control"] or "{}")
                    if "access_control" in row.keys()
                    else {}
                ),
                "observed_at": row["observed_at"],
                "period_start": row["period_start"],
                "period_end": row["period_end"],
                "content_source": (
                    row["content_source"] if "content_source" in row.keys() else "unknown"
                ),
                "user_intent_signal": (
                    row["user_intent_signal"] if "user_intent_signal" in row.keys() else "unknown"
                ),
                "user_notes": row["user_notes"] if "user_notes" in row.keys() else "",
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "version": int(row["version"]) if row["version"] not in (None, "") else 1,
            }
        )


class ObservationIndex:
    """
    Observation Index — Layer 3 唯一真实来源的查询门面

    设计原则：
    - ObservationStore 是系统唯一真实来源，Wiki `09-Observations/` 只是只读投影。
    - 本门面提供稳定的查询接口，隐藏 SQLite 细节。
    - `rebuild_from_sources()` 是系统级调试/清理通道，会清空现有 Index 并从 L1/L2 重新提取。
    """

    def __init__(self, store: Optional[ObservationStore] = None):
        self.store = store or ObservationStore()

    def query(
        self,
        dimension: Optional[Dimension] = None,
        source_type: Optional[SourceType] = None,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Observation]:
        """按维度、来源类型、时间窗口查询 Observation"""
        return self.store.query(
            dimension=dimension,
            source_type=source_type,
            period_start=period_start,
            period_end=period_end,
            limit=limit,
        )

    def get_by_dimension(self, dimension: Dimension, limit: int = 100) -> List[Observation]:
        """获取指定维度的所有 Observation"""
        return self.store.query(dimension=dimension, limit=limit)

    def get_latest(self, limit: int = 20) -> List[Observation]:
        """获取最新更新的 Observation"""
        return self.store.query(limit=limit)

    def authorized_query(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
        dimension: Optional[Dimension] = None,
        source_type: Optional[SourceType] = None,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
        limit: int = 100,
    ) -> tuple[List[Observation], Dict[str, Any]]:
        """Read observations through the ACL-before-payload retrieval seam."""

        return self.store.authorized_query(
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
            dimension=dimension,
            source_type=source_type,
            period_start=period_start,
            period_end=period_end,
            limit=limit,
        )

    def authorized_get_latest(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
        limit: int = 20,
    ) -> tuple[List[Observation], Dict[str, Any]]:
        return self.authorized_query(
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
            limit=limit,
        )

    def get_stats(self) -> Dict:
        """获取 Index 统计"""
        return self.store.get_stats()

    def rebuild_from_sources(
        self,
        raw_events_db: Optional[str] = None,
        wiki_dir: Optional[str] = None,
        backup: bool = True,
    ) -> Dict:
        """
        从 L1/L2 重新构建 Observation Index

        警告：这是破坏性操作，会清空当前 Index 并重新提取。
        仅用于系统调试或清理早期垃圾数据。

        Args:
            raw_events_db: canonical Raw 数据库路径
            wiki_dir: L2 wiki 仓库路径
            backup: 是否先备份当前数据库

        Returns:
            Dict with rebuild stats
        """
        from core.cognitive.sources import SourceReader

        # 1. 备份（可选）

        if backup and self.store.db_path.exists():
            backup_path = self.store.db_path.with_suffix(
                f".backup.{datetime.now().strftime('%Y%m%d%H%M%S')}.db"
            )
            backup_path.write_bytes(self.store.db_path.read_bytes())
        else:
            backup_path = None

        # 2. 清空当前 Index
        self.store.clear_all()

        # 3. 读取所有来源
        reader = SourceReader(
            raw_events_db=raw_events_db,
            wiki_dir=wiki_dir,
            require_canonical_raw=bool(raw_events_db),
        )
        items = list(reader.read_all())

        if not items:
            return {
                "backup_path": str(backup_path) if backup_path else None,
                "source_items": 0,
                "observations": 0,
                "inserted": 0,
                "updated": 0,
                "by_dimension": {},
            }

        # 4. 逐个维度提取（复用 ObservationEngine 公共逻辑）
        observations = _extract_observations(items)

        # 5. 批量写入 Index
        stats = self.store.save_batch(observations)
        by_dimension: Dict[str, int] = {}
        for obs in observations:
            by_dimension[obs.dimension.value] = by_dimension.get(obs.dimension.value, 0) + 1

        return {
            "backup_path": str(backup_path) if backup_path else None,
            "source_items": len(items),
            "observations": len(observations),
            "inserted": stats["inserted"],
            "updated": stats["updated"],
            "by_dimension": by_dimension,
        }
