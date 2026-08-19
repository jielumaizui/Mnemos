"""Authorized search-session feedback recording."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any, List, Mapping, Optional
from uuid import uuid4

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.app.context_search_models import SearchResult
from core.cognitive.feedback_entrypoints import record_context_search_feedback
from core.cognitive.feedback_migration_barrier import FeedbackMigrationInProgress
from core.db_utils import sqlite_conn
from core.ops import search_flow_receipts


logger = logging.getLogger(__name__)


class ContextSearchFeedbackMixin:
    """Persist and authorize click or ignore feedback for search sessions."""

    if TYPE_CHECKING:
        _search_session_db: Path | None
        wiki_base: Path

        def _get_metrics(self) -> Any: ...

    def _get_search_session_db_path(self) -> Path | None:
        """Return only the database path explicitly bound by the search owner."""

        return self._search_session_db

    def _record_search_hits(self, results: List[SearchResult]) -> None:
        if not results:
            return
        metrics = self._get_metrics()
        if not metrics:
            return
        for result in results:
            try:
                metrics.update_heat(result.page_path, access_type="search_hit")
                page = metrics.get_page(result.page_path)
                if page:
                    search_db = self._get_search_session_db_path()
                    if search_db is None:
                        continue
                    database_dir = search_db.parent
                    heat_item_id = search_flow_receipts.start_heat_application(
                        database_dir, result.page_path
                    )
                    result.heat_level = page.heat_level or result.heat_level
                    result.heat_score = float(page.heat_score or result.heat_score)
                    result.last_accessed = page.last_accessed or result.last_accessed
                    search_flow_receipts.finish_heat_application(database_dir, heat_item_id)
            except (OSError, ValueError):
                logger.debug("搜索热力反馈失败: %s", result.page_path, exc_info=True)

    def _record_search_session(
        self,
        query: str,
        results: List[SearchResult],
        *,
        subject_provenance: Mapping[str, Any] | None = None,
    ) -> None:
        """[P2-10] 记录搜索会话，供后续点击/忽略/无结果信号检测。"""
        try:
            db_path = self._get_search_session_db_path()
            if db_path is None:
                return
            # 确保表存在
            from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

            AdaptiveScorerV2.ensure_tables(str(db_path))

            # A search session is an event, not a query cache key.  Reusing a
            # query-derived ID let another principal/scope replace the row
            # while leaving the old immutable provenance sidecar behind.
            session_id = f"search-{uuid4().hex}"
            paths = json.dumps([r.page_path for r in results[:5]])
            now = datetime.now().isoformat()
            outcome_status = "" if results else "no_result"
            with sqlite_conn(str(db_path)) as conn:
                search_cursor = conn.execute(
                    """
                    INSERT INTO search_sessions
                        (session_id, query, result_paths, created_at, outcome_status, outcome_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (session_id, query, paths, now, outcome_status, now if not results else ""),
                )
                from core.scoring.subject_provenance import record_scoring_subject_provenance

                record_scoring_subject_provenance(
                    conn,
                    object_type="search_session",
                    object_id=str(search_cursor.lastrowid),
                    subject_provenance=subject_provenance,
                )
                conn.commit()
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            if subject_provenance is not None:
                raise
            logger.debug("[ContextAwareSearch] 记录搜索会话失败", exc_info=True)

    @staticmethod
    def _authorized_search_feedback_rows(
        conn: sqlite3.Connection,
        headers: List[tuple[Any, ...]],
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ):
        """Yield payload rows only after the scoring ACL header authorizes."""

        if principal is None:
            return
        from core.cognitive.access_control import authorize_cognitive_access
        from core.scoring.subject_provenance import (
            get_scoring_object_access_control,
        )

        for object_id, stored_session_id in headers:
            access_control = get_scoring_object_access_control(
                conn,
                object_type="search_session",
                object_id=str(object_id),
            )
            if access_control is None:
                continue
            decision = authorize_cognitive_access(
                access_control,
                principal=principal,
                narrowing=narrowing or AccessNarrowing(),
                purpose="search_feedback",
            )
            if not decision.allowed:
                continue
            # Query and result_paths can contain private semantic bytes.  They
            # are selected only after the sidecar-only decision above.
            payload = conn.execute(
                """
                SELECT query, result_paths, clicked_path
                FROM search_sessions WHERE id=? AND session_id=?
                """,
                (object_id, stored_session_id),
            ).fetchone()
            if payload is not None:
                yield (
                    int(object_id),
                    str(stored_session_id),
                    str(payload[0] or ""),
                    str(payload[1] or "[]"),
                    str(payload[2] or ""),
                    access_control,
                )

    @staticmethod
    def record_search_click(
        page_path: str,
        db_path: Optional[Path] = None,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> dict[str, Any]:
        """Record a canonical weak click and return its full terminal DTO."""
        try:
            from core.config import get_config
            db = db_path or (get_config().database_dir / "mnemos.db")
            if principal is None:
                return {"success": False, "reason": "principal_required"}
            if not Path(db).is_file():
                return {"success": False, "reason": "search_database_missing"}
            cutoff = (datetime.now() - timedelta(minutes=5)).isoformat()
            with sqlite_conn(str(db)) as conn:
                headers = conn.execute(
                    """
                    SELECT id, session_id
                    FROM search_sessions
                    WHERE created_at > ? AND (clicked_path IS NULL OR clicked_path = '')
                    ORDER BY created_at DESC, id DESC
                """,
                    (cutoff,),
                ).fetchall()
                selected = None
                for candidate in ContextSearchFeedbackMixin._authorized_search_feedback_rows(
                    conn,
                    headers,
                    principal=principal,
                    narrowing=narrowing,
                ):
                    try:
                        paths = json.loads(candidate[3])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if page_path in paths:
                        selected = candidate
                        break
                if selected is None:
                    return {"success": False, "reason": "authorized_search_not_found"}
                object_id, session_id, query, result_paths, _clicked, access_control = selected
            result = record_context_search_feedback(
                database_dir=Path(db).parent,
                search_object_id=object_id,
                search_session_id=session_id,
                query=query,
                result_paths_json=result_paths,
                interaction="click",
                result_path=page_path,
                access_control=access_control,
                principal=principal,
            )
            return result
        except FeedbackMigrationInProgress:
            raise
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.debug("[ContextAwareSearch] 记录搜索点击失败", exc_info=True)
            return {"success": False, "reason": "search_feedback_record_failed"}

    @staticmethod
    def record_search_ignore(
        session_id: Optional[str] = None,
        *,
        db_path: Optional[Path] = None,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> dict[str, Any]:
        """Record canonical weak no-click feedback and return its full DTO."""
        try:
            from core.config import get_config
            db = db_path or (get_config().database_dir / "mnemos.db")
            if principal is None:
                return {"success": False, "reason": "principal_required"}
            if not Path(db).is_file():
                return {"success": False, "reason": "search_database_missing"}
            cutoff = (datetime.now() - timedelta(minutes=5)).isoformat()
            with sqlite_conn(str(db)) as conn:
                if session_id:
                    headers = conn.execute(
                        """
                        SELECT id, session_id
                        FROM search_sessions
                        WHERE session_id = ?
                    """,
                        (session_id,),
                    ).fetchall()
                else:
                    headers = conn.execute(
                        """
                        SELECT id, session_id
                        FROM search_sessions
                        WHERE created_at > ?
                          AND (outcome_status IS NULL OR outcome_status = '')
                          AND (clicked_path IS NULL OR clicked_path = '')
                        ORDER BY created_at DESC, id DESC
                    """,
                        (cutoff,),
                    ).fetchall()
                selected = next(
                    ContextSearchFeedbackMixin._authorized_search_feedback_rows(
                        conn,
                        headers,
                        principal=principal,
                        narrowing=narrowing,
                    ),
                    None,
                )
                if selected is None:
                    return {"success": False, "reason": "authorized_search_not_found"}
                object_id, found_session_id, query, paths, _clicked, access_control = selected
            result = record_context_search_feedback(
                database_dir=Path(db).parent,
                search_object_id=object_id,
                search_session_id=found_session_id,
                query=query,
                result_paths_json=paths,
                interaction="ignore",
                result_path="",
                access_control=access_control,
                principal=principal,
            )
            return result
        except FeedbackMigrationInProgress:
            raise
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.debug("[ContextAwareSearch] 记录搜索忽略失败", exc_info=True)
            return {"success": False, "reason": "search_feedback_record_failed"}
