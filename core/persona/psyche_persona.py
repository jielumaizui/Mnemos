"""Profile queries and governed Persona material mutations for SignalStore."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence

if TYPE_CHECKING:
    from core.db_utils import SqlitePool
    from core.persona.cognitive_profile import CognitiveProfileRepository

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionRequest,
    require_material_action,
    resolve_material_action_recovery_authorization,
)
from core.cognitive.material_effect_ledger import (
    record_target_effect,
    recover_pending_target_effects,
    recover_recorded_target_effect,
)
from core.cognitive.state_contract import sha256_json
from core.db_utils import validate_sql_identifier
from core.persona.cognitive_profile import (
    ProfileAssertion,
    ProfileSignal,
    ProfileUsageLog,
)
from core.persona.psyche_constants import (
    CORE,
    SIGNAL_STORE_DURATION_BUCKET_QUARTER_DAYS,
    SIGNAL_STORE_GET_PROJECT_ISOLATED_SIGNALS_PROJECT_DIR_DAYS,
    SIGNAL_STORE_GET_SIGNAL_PROJECTS_DAYS,
)
from core.persona.psyche_material_contracts import (
    PERSONA_BLINDSPOT_ACTION,
    PERSONA_BLINDSPOT_REVOKE_ACTION,
    PERSONA_CALIBRATION_ACTION,
    PERSONA_CALIBRATION_REVOKE_ACTION,
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


class SignalPersonaMixin:
    """Own profile projections, queries, and governed material mutations."""

    if TYPE_CHECKING:
        db_path: Path
        _pool: SqlitePool
        _cognitive_profiles: CognitiveProfileRepository

    def record_profile_signal(self, signal: ProfileSignal) -> int:
        return self._cognitive_profiles.record_signal(signal)

    def upsert_profile_assertion(self, assertion: ProfileAssertion) -> str:
        return self._cognitive_profiles.upsert_assertion(assertion)

    def record_authorized_profile_evidence(self, **kwargs: Any) -> Dict[str, Any]:
        """Persist Profile v2 only through the exact Raw authority producer."""

        return self._cognitive_profiles.record_authorized_profile_evidence(**kwargs)

    def record_profile_usage(
        self,
        usage: ProfileUsageLog,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing | None,
    ) -> int:
        return self._cognitive_profiles.record_usage(
            usage,
            principal=principal,
            narrowing=narrowing,
        )

    def replay_profile_usage_outbox(self, *, limit: int = 100) -> tuple[str, ...]:
        return self._cognitive_profiles.replay_profile_usage_outbox(limit=limit)

    def get_profile_assertions(
        self,
        status: str = "active",
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = "",
    ) -> List[Dict[str, Any]]:
        return self._cognitive_profiles.get_assertions(
            status=status,
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
        )

    def get_profile_assertion_revisions(self, assertion_id: str) -> List[Dict[str, Any]]:
        return self._cognitive_profiles.get_assertion_revisions(assertion_id)

    def rebuild_profile_assertion_projection(self, assertion_id: str) -> Dict[str, Any]:
        return self._cognitive_profiles.rebuild_profile_assertion_projection(assertion_id)

    def get_profile_usage_metrics(
        self,
        days: int = CORE,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = "",
    ) -> Dict[str, Any]:
        return self._cognitive_profiles.get_authorized_usage_metrics(
            days=days,
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
        )

    def get_authorized_profile_usage_metrics(
        self,
        days: int = CORE,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
    ) -> Dict[str, Any]:
        return self._cognitive_profiles.get_authorized_usage_metrics(
            days=days,
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
        )

    def build_user_cognitive_profile_v2(
        self,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = "",
    ) -> Dict[str, Any]:
        profile, _access = self._cognitive_profiles.build_authorized_profile_v2(
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
        )
        return profile

    def build_authorized_user_cognitive_profile_v2(
        self,
        *,
        principal,
        narrowing,
        purpose: str,
        consumer: str = "",
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Return profile claims only after per-assertion ACL authorization."""

        return self._cognitive_profiles.build_authorized_profile_v2(
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
            consumer=consumer,
        )

    def get_daily_summary(self, date: str) -> Dict[str, Any]:
        """获取某天的信号聚合摘要。"""
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        cursor = conn.execute(
            """
            SELECT * FROM signal_daily_index WHERE date = ?
        """,
            (date,),
        )
        rows = cursor.fetchall()
        if rows:
            return {row["source_type"]: self._daily_index_row_to_dict(row) for row in rows}
        return self._compute_daily_summary(conn, date)

    @staticmethod
    def _daily_index_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        summary_json = data.pop("summary_json", "") or "{}"
        try:
            data["summary"] = json.loads(summary_json)
        except json.JSONDecodeError:
            data["summary"] = {"raw": summary_json}
        return data

    @staticmethod
    def _daily_group_counts(
        conn: sqlite3.Connection,
        table: str,
        timestamp_column: str,
        group_column: str,
        date: str,
    ) -> Dict[str, int]:
        table_name = validate_sql_identifier(table)
        ts_col = validate_sql_identifier(timestamp_column)
        group_col = validate_sql_identifier(group_column)
        cursor = conn.execute(
            f"""
            SELECT {group_col} AS key, COUNT(*) AS count
            FROM {table_name}
            WHERE substr({ts_col}, 1, 10) = ?
              AND {group_col} IS NOT NULL
              AND {group_col} != ''
            GROUP BY {group_col}
            ORDER BY count DESC, {group_col} ASC
            LIMIT 10
        """,  # nosec B608: identifiers are validated before interpolation.
            (date,),
        )
        return {str(row["key"]): row["count"] for row in cursor.fetchall()}

    def _compute_daily_summary(self, conn: sqlite3.Connection, date: str) -> Dict[str, Any]:
        specs = [
            ("session", "session_signals", "timestamp", ["task_type", "agent"]),
            ("knowledge", "knowledge_signals", "timestamp", ["action_type"]),
            ("git", "git_signals", "timestamp", ["commit_type"]),
            ("file_system", "file_system_signals", "timestamp", ["action_type", "project_name"]),
            ("notes", "note_signals", "timestamp", ["is_ai_generated", "ai_agent"]),
            ("wechat", "wechat_signals", "timestamp", ["chat_type"]),
            ("document", "document_signals", "import_timestamp", ["doc_type", "import_source"]),
            ("reflection", "reflection_signals", "timestamp", ["dimension", "source"]),
        ]
        summary: Dict[str, Any] = {}
        for source_type, table, timestamp_column, group_columns in specs:
            table_name = validate_sql_identifier(table)
            ts_col = validate_sql_identifier(timestamp_column)
            cursor = conn.execute(
                f"""
                SELECT COUNT(*) AS signal_count
                FROM {table_name}
                WHERE substr({ts_col}, 1, 10) = ?
            """,  # nosec B608: identifiers are static specs validated before interpolation.
                (date,),
            )
            count = int(cursor.fetchone()["signal_count"])
            if count == 0:
                continue
            summary[source_type] = {
                "date": date,
                "source_type": source_type,
                "signal_count": count,
                "summary": {
                    column: self._daily_group_counts(
                        conn,
                        table,
                        timestamp_column,
                        column,
                        date,
                    )
                    for column in group_columns
                },
            }
        return summary

    # ---- 跨项目隔离 ----

    @staticmethod
    def _escape_like_pattern(value: str) -> str:
        """Escape SQL LIKE wildcards while preserving literal project paths."""
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def get_recent_session_signals_by_project(
        self, working_dir: str, days: int = SIGNAL_STORE_DURATION_BUCKET_QUARTER_DAYS
    ) -> List[Dict]:
        """
        按工作目录（项目）获取session信号。

        用于防止跨项目污染：不同项目的信号可能反映不同的工作偏好，
        不应混为一谈。
        """
        # 转义 SQL LIKE 通配符，防止语义漂移
        escaped = self._escape_like_pattern(working_dir)
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        cursor = conn.execute(
            """
            SELECT * FROM session_signals
            WHERE working_dir LIKE ? ESCAPE '\\'
              AND timestamp >= date('now', ?)
            ORDER BY timestamp DESC
        """,
            (f"%{escaped}%", f"-{days} days"),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_signal_projects(self, days: int = SIGNAL_STORE_GET_SIGNAL_PROJECTS_DAYS) -> List[Dict]:
        """获取所有有信号的项目列表"""
        projects = []
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        # Session项目
        cursor = conn.execute(
            """
            SELECT working_dir, COUNT(*) as count
            FROM session_signals
            WHERE timestamp >= date('now', ?)
              AND working_dir IS NOT NULL
              AND TRIM(working_dir) != ''
            GROUP BY working_dir
            ORDER BY count DESC
        """,
            (f"-{days} days",),
        )
        for row in cursor.fetchall():
            projects.append(
                {
                    "type": "session",
                    "identifier": row["working_dir"],
                    "signal_count": row["count"],
                }
            )
        # Git项目
        cursor = conn.execute(
            """
            SELECT repo_path, COUNT(*) as count
            FROM git_signals
            WHERE timestamp >= date('now', ?)
              AND TRIM(repo_path) != ''
            GROUP BY repo_path
            ORDER BY count DESC
        """,
            (f"-{days} days",),
        )
        for row in cursor.fetchall():
            projects.append(
                {
                    "type": "git",
                    "identifier": row["repo_path"],
                    "signal_count": row["count"],
                }
            )
        return projects

    # ---- 去重检查 ----

    def session_exists(self, session_id: str) -> bool:
        """检查 session 信号是否已存在"""
        conn = self._pool.get_conn()
        cursor = conn.execute(
            "SELECT 1 FROM session_signals WHERE session_id = ? LIMIT 1", (session_id,)
        )
        return cursor.fetchone() is not None

    def git_commit_exists(self, commit_hash: str) -> bool:
        """检查 git commit 信号是否已存在"""
        conn = self._pool.get_conn()
        cursor = conn.execute(
            "SELECT 1 FROM git_signals WHERE commit_hash = ? LIMIT 1", (commit_hash,)
        )
        return cursor.fetchone() is not None

    def note_exists(self, note_uid: str) -> bool:
        """检查笔记信号是否已存在（note_uid 为空时不检查）"""
        if not note_uid:
            return False
        conn = self._pool.get_conn()
        cursor = conn.execute("SELECT 1 FROM note_signals WHERE note_uid = ? LIMIT 1", (note_uid,))
        return cursor.fetchone() is not None

    def knowledge_page_exists(self, page_path: str, since: str | None = None) -> bool:
        """检查知识库页面信号是否已存在"""
        conn = self._pool.get_conn()
        if since:
            cursor = conn.execute(
                "SELECT 1 FROM knowledge_signals WHERE page_path = ? AND timestamp >= ? LIMIT 1",
                (page_path, since),
            )
        else:
            cursor = conn.execute(
                "SELECT 1 FROM knowledge_signals WHERE page_path = ? LIMIT 1", (page_path,)
            )
        return cursor.fetchone() is not None

    def file_system_exists(self, file_path: str, since: str | None = None) -> bool:
        """检查文件系统信号是否已存在"""
        conn = self._pool.get_conn()
        if since:
            cursor = conn.execute(
                "SELECT 1 FROM file_system_signals WHERE file_path = ? AND timestamp >= ? LIMIT 1",
                (file_path, since),
            )
        else:
            cursor = conn.execute(
                "SELECT 1 FROM file_system_signals WHERE file_path = ? LIMIT 1", (file_path,)
            )
        return cursor.fetchone() is not None

    def get_project_isolated_signals(
        self,
        project_dir: str,
        days: int = SIGNAL_STORE_GET_PROJECT_ISOLATED_SIGNALS_PROJECT_DIR_DAYS,
    ) -> Dict[str, List[Dict]]:
        """
        获取单个项目隔离后的所有信号。

        Returns:
            {"session": [...], "git": [...], "file_system": [...]}
        """
        results = {
            "session": self.get_recent_session_signals_by_project(project_dir, days),
        }
        escaped_project_dir = self._escape_like_pattern(project_dir)

        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa

        # Git信号（匹配repo_path前缀）
        cursor = conn.execute(
            """
            SELECT * FROM git_signals
            WHERE repo_path LIKE ? ESCAPE '\\'
              AND timestamp >= date('now', ?)
            ORDER BY timestamp DESC
        """,
            (f"%{escaped_project_dir}%", f"-{days} days"),
        )
        results["git"] = [dict(row) for row in cursor.fetchall()]

        # 文件系统信号
        cursor = conn.execute(
            """
            SELECT * FROM file_system_signals
            WHERE file_path LIKE ? ESCAPE '\\'
              AND timestamp >= date('now', ?)
            ORDER BY timestamp DESC
        """,
            (f"%{escaped_project_dir}%", f"-{days} days"),
        )
        results["file_system"] = [dict(row) for row in cursor.fetchall()]

        return results

    def _persona_material_effect(
        self,
        command_id: str,
        *,
        schema_version: str,
    ) -> dict[str, Any]:
        with sqlite3.connect(
            f"file:{self.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT outcome FROM material_target_effects WHERE command_id=?",
                (str(command_id),),
            ).fetchone()
        if row is None:
            raise RuntimeError("persona material target journal is missing")
        try:
            outcome = json.loads(str(row["outcome"] or ""))
        except json.JSONDecodeError as exc:
            raise RuntimeError("persona material target journal outcome is invalid") from exc
        if not isinstance(outcome, dict) or outcome.get("schema_version") != schema_version:
            raise RuntimeError("persona material target journal outcome is unsupported")
        return outcome

    # ---- Persona 版本管理 ----

    def save_persona_version(
        self,
        version: int,
        period_start: str,
        period_end: str,
        energy: Dict,
        cognitive: Dict,
        value: Dict,
        blindspot: Dict,
        signal_count: int,
        *,
        generated_at: str = "",
        material_action: MaterialActionAuthorization | None = None,
        action_type: str = PERSONA_VERSION_ACTION,
        actor: str = "system",
        reason: str = "signal_store.save_persona_version",
        user_confirmed: bool = False,
        confirmed_at: str = "",
        calibration_score: float | None = None,
        expected_supersedes_revision_id: str = "",
        source_signal_ids: Mapping[str, Sequence[int]] | None = None,
        revision_metadata: Mapping[str, Any] | None = None,
    ) -> int:
        """Commit one immutable canonical Persona revision.

        ``persona_versions`` is deliberately not a runtime write target: it is
        a legacy migration source.  The canonical ledger enforces both one
        semantic version and one content body, while ``persona_revision_heads``
        carries the only current head.
        """
        binding_generated_at = str(generated_at or "material-command-issued-at")
        binding = persona_version_material_action_binding(
            version=version,
            generated_at=binding_generated_at,
            period_start=period_start,
            period_end=period_end,
            energy=energy,
            cognitive=cognitive,
            value=value,
            blindspot=blindspot,
            signal_count=signal_count,
            user_confirmed=user_confirmed,
            confirmed_at=confirmed_at,
            calibration_score=calibration_score,
            supersedes_revision_id=expected_supersedes_revision_id,
            source_signal_ids=source_signal_ids,
            revision_metadata=revision_metadata,
            action_type=action_type,
            actor=actor,
            reason=reason,
        )
        if action_type == PERSONA_VERSION_ACTION:
            effect_oracle = PersonaVersionEffectOracle(self.db_path)
        elif action_type == PERSONA_BLINDSPOT_ACTION:
            effect_oracle = PersonaBlindspotEffectOracle(self.db_path)
        elif action_type == PERSONA_CALIBRATION_ACTION:
            effect_oracle = PersonaCalibrationEffectOracle(self.db_path)
        elif action_type == PERSONA_BLINDSPOT_REVOKE_ACTION:
            effect_oracle = PersonaBlindspotRevokeEffectOracle(self.db_path)
        elif action_type == PERSONA_CALIBRATION_REVOKE_ACTION:
            effect_oracle = PersonaCalibrationRevokeEffectOracle(self.db_path)
        else:
            raise ValueError(f"unsupported canonical Persona revision action: {action_type}")
        recover_pending_target_effects(
            state_db_path=self.db_path.parent / "producer_consumer_ledger.db",
            oracle=effect_oracle,
            target_ref=binding["target_ref"],
        )
        material_action, permit = resolve_material_action_recovery_authorization(
            material_action,
            owner=PERSONA_VERSION_OWNER,
            executor_id=PERSONA_VERSION_EXECUTOR,
            action_type=action_type,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.db_path.parent / "producer_consumer_ledger.db",
        )

        def record_projection(row_id: int) -> None:
            """Record reciprocal provenance for the committed persona version."""

            reciprocal_refs = [
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                f"persona-version:{version}",
                f"persona-row:{row_id}",
            ]
            from core.trust.formal_cognitive_mutation import (
                FormalCognitiveMutationJournal,
            )

            FormalCognitiveMutationJournal.for_database(self.db_path).record(
                asset_kind="persona_profile",
                action=action_type,
                target_ref=binding["target_ref"],
                actor=actor,
                decision=permit.decision_revision_id,
                reason=reason,
                evidence_refs=reciprocal_refs,
                metadata=binding["payload"],
                material_action=material_action,
            )

        if recover_recorded_target_effect(
            material_action,
            effect_oracle,
        ):
            outcome = self._persona_material_effect(
                permit.command_id,
                schema_version="mnemos.persona_revision_effect.v2",
            )
            if outcome.get("binding_payload") != binding["payload"]:
                raise PermissionError("terminal persona command belongs to another exact version")
            row_id = int(outcome["row_id"])
            conn = self._pool.get_conn()
            row = conn.execute(
                "SELECT id FROM persona_revisions WHERE id=? AND version=?",
                (row_id, version),
            ).fetchone()
            if row is None:
                raise RuntimeError("recovered Persona revision has no target row")
            record_projection(row_id)
            return row_id
        permit = require_material_action(
            material_action,
            owner=PERSONA_VERSION_OWNER,
            executor_id=PERSONA_VERSION_EXECUTOR,
            action_type=action_type,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.db_path.parent / "producer_consumer_ledger.db",
        )
        resolved_generated_at = str(generated_at or permit.issued_at)
        content_hash = str(binding["content_hash"])
        revision_id = f"persona-revision:{int(version)}:{content_hash.split(':', 1)[-1]}"
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            source_cursor = dict(binding["payload"]["source_signal_ids"])
            before_signal_cursor = self._persona_signal_cursor_state(
                conn,
                source_cursor,
                require_unprocessed=True,
            )
            before_domain_events = self._persona_domain_event_state(conn)
            before_rows = [
                dict(row)
                for row in conn.execute("SELECT * FROM persona_revisions ORDER BY id").fetchall()
            ]
            before_head = conn.execute(
                "SELECT scope_key, revision_id, updated_at FROM persona_revision_heads "
                "WHERE scope_key='global'"
            ).fetchone()
            before_hash = sha256_json(
                {
                    "persona_revisions": before_rows,
                    "persona_head": dict(before_head) if before_head else None,
                    "signal_cursor": before_signal_cursor,
                    "persona_domain_events": before_domain_events,
                }
            )
            prior_head = str(before_head["revision_id"]) if before_head is not None else None
            if expected_supersedes_revision_id and prior_head != expected_supersedes_revision_id:
                raise ValueError("Persona revision parent changed before the command could commit")
            cursor = conn.execute(
                """
                INSERT INTO persona_revisions (
                    revision_id, version, content_hash, supersedes_revision_id,
                    source_cursor, materiality_evidence, generated_at,
                    period_start, period_end, energy_profile, cognitive_profile,
                    value_profile, blindspot_profile, signal_count_used,
                    user_confirmed, confirmed_at, calibration_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    int(version),
                    content_hash,
                    prior_head,
                    json.dumps(
                        {
                            "schema_version": "mnemos.persona_signal_cursor.v1",
                            "decision_revision_id": permit.decision_revision_id,
                            "source_signal_ids": source_cursor,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(
                        {
                            "decision_revision_id": permit.decision_revision_id,
                            "command_id": permit.command_id,
                            "signal_count": int(signal_count),
                            "source_signal_ids": source_cursor,
                            "revision_metadata": binding["payload"]["revision_metadata"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    resolved_generated_at,
                    period_start,
                    period_end,
                    json.dumps(energy, ensure_ascii=False, sort_keys=True),
                    json.dumps(cognitive, ensure_ascii=False, sort_keys=True),
                    json.dumps(value, ensure_ascii=False, sort_keys=True),
                    json.dumps(blindspot, ensure_ascii=False, sort_keys=True),
                    int(signal_count),
                    int(bool(user_confirmed)),
                    str(confirmed_at) or None,
                    calibration_score,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Persona revision insert did not return a row id")
            row_id = int(cursor.lastrowid)
            observed_at = datetime.now().astimezone().isoformat()
            conn.execute(
                """
                INSERT INTO persona_revision_heads(scope_key, revision_id, updated_at)
                VALUES ('global', ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    revision_id=excluded.revision_id,
                    updated_at=excluded.updated_at
                """,
                (revision_id, observed_at),
            )
            self._consume_persona_signal_cursor(
                conn,
                source_cursor,
                observed_at=observed_at,
            )
            self._append_persona_domain_event(
                conn,
                revision_id=revision_id,
                action_type=action_type,
                payload=binding["payload"],
                observed_at=observed_at,
            )
            after_signal_cursor = self._persona_signal_cursor_state(
                conn,
                source_cursor,
                require_unprocessed=False,
            )
            after_domain_events = self._persona_domain_event_state(conn)
            after_rows = [
                dict(row)
                for row in conn.execute("SELECT * FROM persona_revisions ORDER BY id").fetchall()
            ]
            after_head = conn.execute(
                "SELECT scope_key, revision_id, updated_at FROM persona_revision_heads "
                "WHERE scope_key='global'"
            ).fetchone()
            after_hash = sha256_json(
                {
                    "persona_revisions": after_rows,
                    "persona_head": dict(after_head) if after_head else None,
                    "signal_cursor": after_signal_cursor,
                    "persona_domain_events": after_domain_events,
                }
            )
            record_target_effect(
                conn,
                permit,
                status="committed",
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=(
                    f"target-after:{after_hash}",
                    f"target-journal:persona-revision:{row_id}:{after_hash}",
                ),
                outcome=json.dumps(
                    {
                        "schema_version": "mnemos.persona_revision_effect.v2",
                        "row_id": row_id,
                        "revision_id": revision_id,
                        "binding_payload": binding["payload"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                observed_at=observed_at,
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            conflicting_version = conn.execute(
                "SELECT revision_id FROM persona_revisions WHERE version=?",
                (int(version),),
            ).fetchone()
            if conflicting_version is not None:
                raise ValueError(
                    f"Persona version {version} already belongs to a different Persona revision"
                ) from exc
            raise ValueError("Persona content is already committed by another revision") from exc
        except BaseException:
            conn.rollback()
            raise
        if not recover_recorded_target_effect(
            material_action,
            effect_oracle,
        ):
            raise RuntimeError("persona version effect journal was not recoverable")
        record_projection(row_id)
        return row_id

    @staticmethod
    def _persona_signal_cursor_state(
        conn: sqlite3.Connection,
        source_cursor: Mapping[str, Sequence[int]],
        *,
        require_unprocessed: bool,
    ) -> list[dict[str, Any]]:
        """Read the exact cursor state and reject stale or missing signals."""

        state: list[dict[str, Any]] = []
        for source_type in sorted(source_cursor):
            signal_ids = [int(signal_id) for signal_id in source_cursor[source_type]]
            if not signal_ids:
                continue
            placeholders = ",".join("?" for _ in signal_ids)
            rows = conn.execute(
                f"""
                SELECT signal_table, signal_id, processed, processed_at
                FROM signal_metadata
                WHERE signal_table=? AND signal_id IN ({placeholders})
                ORDER BY signal_id
                """,  # nosec B608: source and placeholders are bound/canonical.
                (source_type, *signal_ids),
            ).fetchall()
            if len(rows) != len(signal_ids):
                raise ValueError("Persona signal cursor contains missing metadata")
            row_state = [dict(row) for row in rows]
            if require_unprocessed and any(int(row["processed"]) != 0 for row in rows):
                raise ValueError("Persona signal cursor was already consumed")
            state.extend(row_state)
        return state

    @classmethod
    def _consume_persona_signal_cursor(
        cls,
        conn: sqlite3.Connection,
        source_cursor: Mapping[str, Sequence[int]],
        *,
        observed_at: str,
    ) -> None:
        """Consume exactly the command-bound cursor inside its revision transaction."""

        cls._persona_signal_cursor_state(
            conn,
            source_cursor,
            require_unprocessed=True,
        )
        for source_type in sorted(source_cursor):
            signal_ids = [int(signal_id) for signal_id in source_cursor[source_type]]
            if not signal_ids:
                continue
            placeholders = ",".join("?" for _ in signal_ids)
            cursor = conn.execute(
                f"""
                UPDATE signal_metadata
                SET processed=1, processed_at=?
                WHERE signal_table=? AND processed=0 AND signal_id IN ({placeholders})
                """,  # nosec B608: source and placeholders are bound/canonical.
                (observed_at, source_type, *signal_ids),
            )
            if cursor.rowcount != len(signal_ids):
                raise RuntimeError("Persona signal cursor changed during commit")

    @staticmethod
    def _persona_domain_event_state(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
        """Return independent append-only Persona event streams for target hashing."""

        return {
            table: [
                dict(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY event_id").fetchall()
            ]
            for table in ("persona_blindspot_events", "persona_calibration_events")
        }

    @staticmethod
    def _append_persona_domain_event(
        conn: sqlite3.Connection,
        *,
        revision_id: str,
        action_type: str,
        payload: Mapping[str, Any],
        observed_at: str,
    ) -> None:
        """Append the domain event paired with a non-base semantic revision."""

        event_specs = {
            PERSONA_BLINDSPOT_ACTION: ("persona_blindspot_events", "blindspot", "applied"),
            PERSONA_BLINDSPOT_REVOKE_ACTION: (
                "persona_blindspot_events",
                "blindspot",
                "revoked",
            ),
            PERSONA_CALIBRATION_ACTION: (
                "persona_calibration_events",
                "calibration",
                "applied",
            ),
            PERSONA_CALIBRATION_REVOKE_ACTION: (
                "persona_calibration_events",
                "calibration",
                "revoked",
            ),
        }
        spec = event_specs.get(action_type)
        if spec is None:
            return
        table, domain, event_type = spec
        previous = conn.execute(
            f"SELECT event_id FROM {table} ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        supersedes_event_id = str(previous["event_id"]) if previous is not None else None
        event_id = f"persona-{domain}-event:{revision_id}"
        event_payload = {
            "schema_version": "mnemos.persona_domain_event.v1",
            "event_id": event_id,
            "revision_id": revision_id,
            "supersedes_event_id": supersedes_event_id,
            "event_type": event_type,
            "action_type": action_type,
            "state": {
                "blindspot": dict(payload["blindspot"]),
                "user_confirmed": bool(payload["user_confirmed"]),
                "confirmed_at": str(payload["confirmed_at"]),
                "calibration_score": payload["calibration_score"],
            },
            "revision_metadata": dict(payload["revision_metadata"]),
        }
        conn.execute(
            f"""
            INSERT INTO {table}(
                event_id, revision_id, supersedes_event_id, event_type,
                payload_json, payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                revision_id,
                supersedes_event_id,
                event_type,
                json.dumps(event_payload, ensure_ascii=False, sort_keys=True),
                sha256_json(event_payload),
                observed_at,
            ),
        )

    def get_latest_persona_version(self) -> Optional[Dict]:
        """Read the one canonical current Persona through its durable head."""

        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT revision.*
            FROM persona_revision_heads AS head
            JOIN persona_revisions AS revision ON revision.revision_id=head.revision_id
            WHERE head.scope_key='global'
            """).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in (
            "energy_profile",
            "cognitive_profile",
            "value_profile",
            "blindspot_profile",
        ):
            result[field] = json.loads(result[field] or "{}")
        return result

    def get_recent_persona_versions(self, limit: int = 2) -> List[Dict]:
        """Return immutable canonical Persona revisions, newest semantic version first."""
        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row  # noqa
        cursor = conn.execute(
            """
            SELECT * FROM persona_revisions
            ORDER BY version DESC LIMIT ?
        """,
            (limit,),
        )
        results = []
        for row in cursor.fetchall():
            result = dict(row)
            result["energy_profile"] = json.loads(result["energy_profile"] or "{}")
            result["cognitive_profile"] = json.loads(result["cognitive_profile"] or "{}")
            result["value_profile"] = json.loads(result["value_profile"] or "{}")
            result["blindspot_profile"] = json.loads(result["blindspot_profile"] or "{}")
            results.append(result)
        return results

    def rebuild_current_persona_state(self) -> Dict[str, Any]:
        """Rebuild and verify the current Persona solely from immutable rows.

        This intentionally does not consult the mutable Wiki projection or a
        legacy ``persona_versions`` row.  Each chain member is re-hashed from
        its persisted input and every domain event is checked against its
        semantic revision before the current-state hash is reported.
        """

        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row
        head = conn.execute(
            "SELECT revision_id FROM persona_revision_heads WHERE scope_key='global'"
        ).fetchone()
        if head is None:
            return {
                "schema_version": "mnemos.persona_current_rebuild.v1",
                "status": "uninitialized",
                "state_hash": "",
                "revision_id": "",
            }
        event_actions: dict[str, str] = {}
        event_payload_by_revision: dict[str, dict[str, Any]] = {}
        event_payloads: list[dict[str, Any]] = []
        for table in ("persona_blindspot_events", "persona_calibration_events"):
            for event_row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall():
                event = dict(event_row)
                try:
                    payload = json.loads(str(event["payload_json"]))
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Persona domain event payload is invalid") from exc
                if sha256_json(payload) != str(event["payload_hash"]):
                    raise RuntimeError("Persona domain event hash does not match payload")
                revision_id = str(event["revision_id"])
                if (
                    str(payload.get("event_id") or "") != str(event["event_id"])
                    or str(payload.get("revision_id") or "") != revision_id
                    or str(payload.get("supersedes_event_id") or "")
                    != str(event["supersedes_event_id"] or "")
                    or str(payload.get("event_type") or "") != str(event["event_type"])
                ):
                    raise RuntimeError("Persona domain event columns do not match payload")
                action_type = str(payload.get("action_type") or "")
                if revision_id in event_actions:
                    raise RuntimeError("Persona revision has more than one domain event")
                event_actions[revision_id] = action_type
                event_payload_by_revision[revision_id] = payload
                event_payloads.append(payload)

        chain: list[sqlite3.Row] = []
        seen: set[str] = set()
        revision_id = str(head["revision_id"])
        while revision_id:
            if revision_id in seen:
                raise RuntimeError("Persona revision chain contains a cycle")
            seen.add(revision_id)
            row = conn.execute(
                "SELECT * FROM persona_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Persona head references a missing revision")
            chain.append(row)
            revision_id = str(row["supersedes_revision_id"] or "")
        chain.reverse()

        current: dict[str, Any] = {}
        for row in chain:
            try:
                source_cursor = json.loads(str(row["source_cursor"]))
                materiality_evidence = json.loads(str(row["materiality_evidence"]))
            except json.JSONDecodeError as exc:
                raise RuntimeError("Persona revision identity metadata is invalid") from exc
            if not isinstance(source_cursor, dict) or not isinstance(materiality_evidence, dict):
                raise RuntimeError("Persona revision identity metadata is unsupported")
            action_type = event_actions.get(str(row["revision_id"]), PERSONA_VERSION_ACTION)
            binding = persona_version_material_action_binding(
                version=int(row["version"]),
                generated_at=str(row["generated_at"]),
                period_start=str(row["period_start"] or ""),
                period_end=str(row["period_end"] or ""),
                energy=json.loads(str(row["energy_profile"] or "{}")),
                cognitive=json.loads(str(row["cognitive_profile"] or "{}")),
                value=json.loads(str(row["value_profile"] or "{}")),
                blindspot=json.loads(str(row["blindspot_profile"] or "{}")),
                signal_count=int(row["signal_count_used"]),
                user_confirmed=bool(row["user_confirmed"]),
                confirmed_at=str(row["confirmed_at"] or ""),
                calibration_score=row["calibration_score"],
                supersedes_revision_id=str(row["supersedes_revision_id"] or ""),
                source_signal_ids=source_cursor.get("source_signal_ids") or {},
                revision_metadata=materiality_evidence.get("revision_metadata") or {},
                action_type=action_type,
            )
            if binding["content_hash"] != str(row["content_hash"]):
                raise RuntimeError("Persona revision content hash does not rebuild")
            domain_event = event_payload_by_revision.get(str(row["revision_id"]))
            if domain_event is not None:
                expected_event_state = {
                    "blindspot": json.loads(str(row["blindspot_profile"] or "{}")),
                    "user_confirmed": bool(row["user_confirmed"]),
                    "confirmed_at": str(row["confirmed_at"] or ""),
                    "calibration_score": row["calibration_score"],
                }
                if domain_event.get("state") != expected_event_state:
                    raise RuntimeError("Persona domain event state does not match revision")
                if domain_event.get("revision_metadata") != (
                    materiality_evidence.get("revision_metadata") or {}
                ):
                    raise RuntimeError("Persona domain event metadata does not match revision")
            current = {
                "revision_id": str(row["revision_id"]),
                "version": int(row["version"]),
                "energy": json.loads(str(row["energy_profile"] or "{}")),
                "cognitive": json.loads(str(row["cognitive_profile"] or "{}")),
                "value": json.loads(str(row["value_profile"] or "{}")),
                "blindspot": json.loads(str(row["blindspot_profile"] or "{}")),
                "user_confirmed": bool(row["user_confirmed"]),
                "confirmed_at": str(row["confirmed_at"] or ""),
                "calibration_score": row["calibration_score"],
                "content_hash": str(row["content_hash"]),
            }
        return {
            "schema_version": "mnemos.persona_current_rebuild.v1",
            "status": "verified",
            "revision_id": str(head["revision_id"]),
            "state": current,
            "state_hash": sha256_json(
                {
                    "current": current,
                    "domain_events": event_payloads,
                }
            ),
        }

    def prepare_blindspot_material_action(
        self,
        blindspot_data: Dict,
        *,
        source_facts: Dict[str, Any],
        evidence_refs: tuple[str, ...],
        created_at: str,
    ) -> MaterialActionAuthorization | None:
        """Seal an immutable blindspot successor before it can be persisted."""

        candidate = self._blindspot_revision_candidate(blindspot_data)
        if candidate is None:
            return None
        binding = persona_version_material_action_binding(
            **candidate,
            action_type=PERSONA_BLINDSPOT_ACTION,
            actor="system",
            reason="signal_store.update_blindspot_profile",
        )
        state_db_path = self.db_path.parent / "producer_consumer_ledger.db"
        request = MaterialActionRequest(
            owner=PERSONA_VERSION_OWNER,
            executor_id=PERSONA_VERSION_EXECUTOR,
            action_type=PERSONA_BLINDSPOT_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=str(state_db_path.resolve(strict=False)),
        )
        return authorize_exact_persona_material_action(
            expected_request=request,
            state_db_path=state_db_path,
            source_namespace="persona-blindspot-update",
            source_facts={
                "schema_version": "mnemos.persona_blindspot_decision_facts.v1",
                "binding_payload": binding["payload"],
                **dict(source_facts),
            },
            evidence_refs=evidence_refs,
            task=f"Append Persona blindspot revision {candidate['version']}",
            goal="Persist only the exact blindspot profile accepted by its workflow.",
            constraints=(
                "The latest Persona row and full blindspot payload must remain exact.",
                "Challenge evidence and counters cannot drift before commit.",
            ),
            created_at=created_at,
            producer="blindspot-profile-manager",
            evaluator_id="persona-blindspot-update-evaluator",
            approved_candidate_key="persist_exact_blindspot_profile",
            approved_candidate_summary=(
                "Persist the exact blindspot profile derived from current evidence."
            ),
            rejected_candidate_key="retain_previous_blindspot_profile",
            rejected_candidate_summary=(
                "Retain the previous blindspot profile when evidence or counters drift."
            ),
            committed_metric="persona_blindspot_committed",
            rejected_metric="unbound_persona_blindspot_count",
        )

    def _blindspot_revision_candidate(
        self,
        blindspot_data: Dict,
    ) -> dict[str, Any] | None:
        """Clone the current head into an immutable blindspot successor."""

        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row
        current = conn.execute("""
            SELECT revision.*
            FROM persona_revision_heads AS head
            JOIN persona_revisions AS revision ON revision.revision_id=head.revision_id
            WHERE head.scope_key='global'
            """).fetchone()
        if current is None:
            return None
        next_version = int(
            conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM persona_revisions").fetchone()[
                0
            ]
        )
        return {
            "version": next_version,
            "generated_at": "material-command-issued-at",
            "period_start": str(current["period_start"] or ""),
            "period_end": str(current["period_end"] or ""),
            "energy": json.loads(str(current["energy_profile"] or "{}")),
            "cognitive": json.loads(str(current["cognitive_profile"] or "{}")),
            "value": json.loads(str(current["value_profile"] or "{}")),
            "blindspot": dict(blindspot_data),
            "signal_count": int(current["signal_count_used"] or 0),
            "user_confirmed": bool(current["user_confirmed"]),
            "confirmed_at": str(current["confirmed_at"] or ""),
            "calibration_score": current["calibration_score"],
            "supersedes_revision_id": str(current["revision_id"]),
            "revision_metadata": self._domain_event_metadata(
                conn,
                table="persona_blindspot_events",
                event_type="applied",
                subject_revision_id=str(current["revision_id"]),
            ),
        }

    def _calibration_revision_candidate(
        self,
        *,
        version: int,
        confirmed_at: str,
        calibration_score: float,
        calibration_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Clone the durable current head into its calibration successor."""

        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row
        current = conn.execute("""
            SELECT revision.*
            FROM persona_revision_heads AS head
            JOIN persona_revisions AS revision ON revision.revision_id=head.revision_id
            WHERE head.scope_key='global'
            """).fetchone()
        if current is None or int(current["version"]) != int(version):
            return None
        next_version = int(
            conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM persona_revisions").fetchone()[
                0
            ]
        )
        return {
            "version": next_version,
            "generated_at": str(confirmed_at),
            "period_start": str(current["period_start"] or ""),
            "period_end": str(current["period_end"] or ""),
            "energy": json.loads(str(current["energy_profile"] or "{}")),
            "cognitive": json.loads(str(current["cognitive_profile"] or "{}")),
            "value": json.loads(str(current["value_profile"] or "{}")),
            "blindspot": json.loads(str(current["blindspot_profile"] or "{}")),
            "signal_count": int(current["signal_count_used"] or 0),
            "user_confirmed": True,
            "confirmed_at": str(confirmed_at),
            "calibration_score": float(calibration_score),
            "supersedes_revision_id": str(current["revision_id"]),
            "revision_metadata": {
                **self._domain_event_metadata(
                    conn,
                    table="persona_calibration_events",
                    event_type="applied",
                    subject_revision_id=str(current["revision_id"]),
                ),
                "calibration": dict(calibration_metadata or {}),
            },
        }

    @staticmethod
    def _domain_event_metadata(
        conn: sqlite3.Connection,
        *,
        table: str,
        event_type: str,
        subject_revision_id: str,
        revoked_event_id: str = "",
    ) -> dict[str, str]:
        """Bind a correction/revocation to the exact prior domain event."""

        latest = conn.execute(
            f"SELECT event_id FROM {table} ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        metadata = {
            "event_type": event_type,
            "subject_revision_id": subject_revision_id,
        }
        if latest is not None:
            metadata["supersedes_event_id"] = str(latest["event_id"])
        if revoked_event_id:
            metadata["revoked_event_id"] = revoked_event_id
        return metadata

    def _domain_revocation_candidate(
        self,
        *,
        table: str,
        generated_at: str,
    ) -> dict[str, Any] | None:
        """Create a successor that restores the state before the current event.

        A historical event may only be revoked while it remains the current
        head.  Rewriting an older event after newer semantic changes would
        create an ambiguous branch, so callers must issue a corrective event
        against the current state instead.
        """

        conn = self._pool.get_conn()
        conn.row_factory = sqlite3.Row
        current = conn.execute("""
            SELECT revision.*
            FROM persona_revision_heads AS head
            JOIN persona_revisions AS revision ON revision.revision_id=head.revision_id
            WHERE head.scope_key='global'
            """).fetchone()
        if current is None:
            return None
        latest_event = conn.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
        if latest_event is None or str(latest_event["revision_id"]) != str(current["revision_id"]):
            raise ValueError("only the current Persona domain event may be revoked")
        if str(latest_event["event_type"]) != "applied":
            raise ValueError("Persona domain event is already revoked")
        parent_id = str(current["supersedes_revision_id"] or "")
        if not parent_id:
            raise ValueError("Persona domain event has no prior state to restore")
        parent = conn.execute(
            "SELECT * FROM persona_revisions WHERE revision_id=?",
            (parent_id,),
        ).fetchone()
        if parent is None:
            raise RuntimeError("Persona domain event parent is missing")
        next_version = int(
            conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM persona_revisions").fetchone()[
                0
            ]
        )
        return {
            "version": next_version,
            "generated_at": generated_at,
            "period_start": str(parent["period_start"] or ""),
            "period_end": str(parent["period_end"] or ""),
            "energy": json.loads(str(parent["energy_profile"] or "{}")),
            "cognitive": json.loads(str(parent["cognitive_profile"] or "{}")),
            "value": json.loads(str(parent["value_profile"] or "{}")),
            "blindspot": json.loads(str(parent["blindspot_profile"] or "{}")),
            "signal_count": int(parent["signal_count_used"] or 0),
            "user_confirmed": bool(parent["user_confirmed"]),
            "confirmed_at": str(parent["confirmed_at"] or ""),
            "calibration_score": parent["calibration_score"],
            "supersedes_revision_id": str(current["revision_id"]),
            "revision_metadata": self._domain_event_metadata(
                conn,
                table=table,
                event_type="revoked",
                subject_revision_id=str(parent["revision_id"]),
                revoked_event_id=str(latest_event["event_id"]),
            ),
        }

    def prepare_persona_calibration_material_action(
        self,
        *,
        version: int,
        confirmed_at: str,
        calibration_score: float,
        calibration_metadata: Mapping[str, Any] | None = None,
        source_facts: Dict[str, Any],
        evidence_refs: tuple[str, ...],
    ) -> MaterialActionAuthorization | None:
        """Seal an explicit user calibration as the next Persona revision."""

        candidate = self._calibration_revision_candidate(
            version=int(version),
            confirmed_at=confirmed_at,
            calibration_score=calibration_score,
            calibration_metadata=calibration_metadata,
        )
        if candidate is None:
            return None
        binding = persona_version_material_action_binding(
            **candidate,
            action_type=PERSONA_CALIBRATION_ACTION,
            actor="user",
            reason="signal_store.record_persona_calibration",
        )
        state_db_path = self.db_path.parent / "producer_consumer_ledger.db"
        request = MaterialActionRequest(
            owner=PERSONA_VERSION_OWNER,
            executor_id=PERSONA_VERSION_EXECUTOR,
            action_type=PERSONA_CALIBRATION_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=str(state_db_path.resolve(strict=False)),
        )
        return authorize_exact_persona_material_action(
            expected_request=request,
            state_db_path=state_db_path,
            source_namespace="persona-user-calibration",
            source_facts={
                "schema_version": "mnemos.persona_calibration_decision_facts.v1",
                "binding_payload": binding["payload"],
                **dict(source_facts),
            },
            evidence_refs=evidence_refs,
            task=f"Calibrate Persona revision {candidate['version']}",
            goal="Apply only the exact calibration explicitly supplied by the user.",
            constraints=(
                "The Persona version and exact calibration score must remain bound.",
                "Only ratings collected in the current interactive calibration apply.",
            ),
            created_at=confirmed_at,
            producer="persona-calibration-cli",
            evaluator_id="persona-user-calibration-evaluator",
            approved_candidate_key="apply_exact_user_calibration",
            approved_candidate_summary=(
                "Apply the exact user-confirmed calibration to the bound Persona version."
            ),
            rejected_candidate_key="retain_uncalibrated_persona_state",
            rejected_candidate_summary=(
                "Retain current Persona state if version, score, or ratings drift."
            ),
            committed_metric="persona_calibration_committed",
            rejected_metric="unbound_persona_calibration_count",
        )

    def _prepare_persona_domain_revocation_material_action(
        self,
        *,
        candidate: Mapping[str, Any],
        action_type: str,
        actor: str,
        reason: str,
        source_namespace: str,
        source_facts: Mapping[str, Any],
        evidence_refs: tuple[str, ...],
        created_at: str,
    ) -> MaterialActionAuthorization:
        """Seal one exact append-only revocation before state can change."""

        binding = persona_version_material_action_binding(
            **candidate,
            action_type=action_type,
            actor=actor,
            reason=reason,
        )
        state_db_path = self.db_path.parent / "producer_consumer_ledger.db"
        request = MaterialActionRequest(
            owner=PERSONA_VERSION_OWNER,
            executor_id=PERSONA_VERSION_EXECUTOR,
            action_type=action_type,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=str(state_db_path.resolve(strict=False)),
        )
        return authorize_exact_persona_material_action(
            expected_request=request,
            state_db_path=state_db_path,
            source_namespace=source_namespace,
            source_facts={
                "schema_version": "mnemos.persona_domain_revoke_facts.v1",
                "binding_payload": binding["payload"],
                **dict(source_facts),
            },
            evidence_refs=evidence_refs,
            task=f"Revoke Persona domain event in revision {candidate['version']}",
            goal="Append the exact Persona state that revokes the current domain event.",
            constraints=(
                "Only the current domain event may be revoked.",
                "The previous immutable Persona state and revocation evidence must remain bound.",
            ),
            created_at=created_at,
            producer="persona-domain-revoke",
            evaluator_id="persona-domain-revoke-evaluator",
            approved_candidate_key="append_exact_persona_domain_revocation",
            approved_candidate_summary="Append the exact state restoring the predecessor domain value.",
            rejected_candidate_key="retain_current_persona_domain_event",
            rejected_candidate_summary="Retain the current event when revocation evidence or parent state drift.",
            committed_metric="persona_domain_revocation_committed",
            rejected_metric="unbound_persona_domain_revocation_count",
        )

    def prepare_persona_calibration_revoke_material_action(
        self,
        *,
        revoked_at: str,
        source_facts: Mapping[str, Any],
        evidence_refs: tuple[str, ...],
    ) -> MaterialActionAuthorization | None:
        """Prepare an append-only revocation of the current calibration event."""

        timestamp = datetime.fromisoformat(str(revoked_at))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("persona calibration revoked_at must include timezone")
        candidate = self._domain_revocation_candidate(
            table="persona_calibration_events",
            generated_at=timestamp.isoformat(),
        )
        if candidate is None:
            return None
        return self._prepare_persona_domain_revocation_material_action(
            candidate=candidate,
            action_type=PERSONA_CALIBRATION_REVOKE_ACTION,
            actor="user",
            reason="signal_store.revoke_persona_calibration",
            source_namespace="persona-user-calibration-revoke",
            source_facts=source_facts,
            evidence_refs=evidence_refs,
            created_at=timestamp.isoformat(),
        )

    def prepare_persona_blindspot_revoke_material_action(
        self,
        *,
        revoked_at: str,
        source_facts: Mapping[str, Any],
        evidence_refs: tuple[str, ...],
    ) -> MaterialActionAuthorization | None:
        """Prepare an append-only revocation of the current blindspot event."""

        timestamp = datetime.fromisoformat(str(revoked_at))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("persona blindspot revoked_at must include timezone")
        candidate = self._domain_revocation_candidate(
            table="persona_blindspot_events",
            generated_at=timestamp.isoformat(),
        )
        if candidate is None:
            return None
        return self._prepare_persona_domain_revocation_material_action(
            candidate=candidate,
            action_type=PERSONA_BLINDSPOT_REVOKE_ACTION,
            actor="system",
            reason="signal_store.revoke_persona_blindspot",
            source_namespace="persona-blindspot-revoke",
            source_facts=source_facts,
            evidence_refs=evidence_refs,
            created_at=timestamp.isoformat(),
        )

    def update_blindspot_profile(
        self,
        blindspot_data: Dict,
        *,
        material_action: MaterialActionAuthorization | None = None,
    ) -> bool:
        """Append a blindspot successor; never mutate an existing revision."""

        candidate = self._blindspot_revision_candidate(blindspot_data)
        if candidate is None:
            return False
        expected_parent = str(candidate.pop("supersedes_revision_id"))
        self.save_persona_version(
            **candidate,
            material_action=material_action,
            action_type=PERSONA_BLINDSPOT_ACTION,
            actor="system",
            reason="signal_store.update_blindspot_profile",
            expected_supersedes_revision_id=expected_parent,
        )
        return True

    def record_persona_calibration(
        self,
        *,
        version: int,
        confirmed_at: str,
        calibration_score: float,
        calibration_metadata: Mapping[str, Any] | None = None,
        material_action: MaterialActionAuthorization | None = None,
    ) -> bool:
        """Persist one explicit user calibration through the Persona owner."""

        timestamp = datetime.fromisoformat(str(confirmed_at))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("persona calibration confirmed_at must include timezone")
        score = float(calibration_score)
        if not 1.0 <= score <= 5.0:
            raise ValueError("persona calibration score must be between 1 and 5")
        candidate = self._calibration_revision_candidate(
            version=int(version),
            confirmed_at=timestamp.isoformat(),
            calibration_score=score,
            calibration_metadata=calibration_metadata,
        )
        if candidate is None:
            return False
        expected_parent = str(candidate.pop("supersedes_revision_id"))
        self.save_persona_version(
            **candidate,
            material_action=material_action,
            action_type=PERSONA_CALIBRATION_ACTION,
            actor="user",
            reason="signal_store.record_persona_calibration",
            expected_supersedes_revision_id=expected_parent,
        )
        return True

    def revoke_persona_calibration(
        self,
        *,
        revoked_at: str,
        material_action: MaterialActionAuthorization | None = None,
    ) -> bool:
        """Append a state that revokes, rather than overwrites, calibration."""

        timestamp = datetime.fromisoformat(str(revoked_at))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("persona calibration revoked_at must include timezone")
        candidate = self._domain_revocation_candidate(
            table="persona_calibration_events",
            generated_at=timestamp.isoformat(),
        )
        if candidate is None:
            return False
        expected_parent = str(candidate.pop("supersedes_revision_id"))
        self.save_persona_version(
            **candidate,
            material_action=material_action,
            action_type=PERSONA_CALIBRATION_REVOKE_ACTION,
            actor="user",
            reason="signal_store.revoke_persona_calibration",
            expected_supersedes_revision_id=expected_parent,
        )
        return True

    def revoke_persona_blindspot(
        self,
        *,
        revoked_at: str,
        material_action: MaterialActionAuthorization | None = None,
    ) -> bool:
        """Append a state that revokes, rather than overwrites, blindspot data."""

        timestamp = datetime.fromisoformat(str(revoked_at))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("persona blindspot revoked_at must include timezone")
        candidate = self._domain_revocation_candidate(
            table="persona_blindspot_events",
            generated_at=timestamp.isoformat(),
        )
        if candidate is None:
            return False
        expected_parent = str(candidate.pop("supersedes_revision_id"))
        self.save_persona_version(
            **candidate,
            material_action=material_action,
            action_type=PERSONA_BLINDSPOT_REVOKE_ACTION,
            actor="system",
            reason="signal_store.revoke_persona_blindspot",
            expected_supersedes_revision_id=expected_parent,
        )
        return True
