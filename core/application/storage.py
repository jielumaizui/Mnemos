# -*- coding: utf-8 -*-
"""Storage application service used by the integration facade."""

from __future__ import annotations

import logging
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.access_policy import AccessNarrowing, PrincipalEnvelope, filter_authorized_items


class StorageApplicationService:
    """Default implementation for storage-backed facade operations."""

    def __init__(
        self,
        storage_backend: Callable[[], Any],
        logger: logging.Logger | None = None,
        receipt_factory: Callable[..., Dict[str, Any]] | None = None,
    ):
        self._storage_backend = storage_backend
        self._logger = logger or logging.getLogger(__name__)
        self._receipt_factory = receipt_factory

    def session_search(
        self,
        query: str = "",
        session_id: str = "",
        uid: str = "",
        limit: int = 10,
        days: Optional[int] = None,
        source: Optional[str] = None,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        """Search canonical raw revisions; projections are candidate hints only."""
        from core.app.raw_search import RawIndex
        from core.config import get_config
        from core.sync_framework.raw_event_store import RawEventStore

        config = get_config()

        try:
            configured_db = config.get("raw_event_store.db_path")
            raw_db = Path(configured_db or (config.database_dir / "raw_events.db")).expanduser()
            if not raw_db.exists():
                return {
                    "success": False,
                    "message": "canonical raw event store unavailable",
                    "canonical_db": str(raw_db),
                }

            store = RawEventStore(db_path=raw_db, config=config)
            try:
                exact_header = None
                if uid and not session_id:
                    if uid.startswith("raw-revision:"):
                        exact_header = store.get_revision_header(
                            uid.removeprefix("raw-revision:")
                        )
                    else:
                        logical_event_id = uid.removeprefix("raw-event:")
                        exact_header = store.get_current_event_header(logical_event_id)
                    if exact_header is None:
                        return {
                            "success": False,
                            "message": "uid is not a canonical Raw event or revision",
                            "uid_resolution": "metadata_only_fail_closed",
                        }

                if exact_header is not None:
                    headers = [exact_header] if exact_header else []
                    if headers:
                        session_id = str(headers[0]["session_id"])
                    if source:
                        headers = [
                            item for item in headers if item["source_agent"] == source
                        ]
                else:
                    headers = store.list_current_headers(
                        session_id=session_id,
                        source_agent=source or "",
                        days=days,
                    )
                effective_narrowing = AccessNarrowing(
                    session_id=session_id or narrowing.session_id,
                    project=narrowing.project,
                )
                authorized_headers, access_summary = filter_authorized_items(
                    headers,
                    principal,
                    effective_narrowing,
                )
                allowed_identities = {
                    (item["source_agent"], item["session_id"])
                    for item in authorized_headers
                }
                projection_candidates: set[tuple[str, str]] = set()
                index_db = config.database_dir / "raw_index.db"
                if index_db.exists() and (query or session_id):
                    idx = RawIndex(
                        raw_dir=config.obsidian_vault_path,
                        db_path=index_db,
                        config=config,
                        read_only=True,
                    )
                    try:
                        projection_candidates = {
                            (item.source, item.session_id)
                            for item in idx.search(
                                query=query,
                                session_id=session_id or None,
                                days=days,
                                source=source,
                                limit=max(10, limit * 3),
                                allowed_identities=allowed_identities,
                            )
                        }
                    finally:
                        idx.close()

                serialized = []
                query_lower = query.casefold()
                for header in authorized_headers:
                    turn = store.get_turn(header["revision_id"])
                    if turn is None:
                        continue
                    text = self._canonical_turn_text(turn)
                    if query_lower and query_lower not in text.casefold():
                        continue
                    snippet, matched_line, line_number = self._canonical_snippet(
                        text, query_lower
                    )
                    is_projection_candidate = (
                        header["source_agent"], header["session_id"]
                    ) in projection_candidates
                    timestamp = header["conversation_at"] or header["captured_at"]
                    serialized.append(
                        {
                            **header,
                            "date": timestamp[:10],
                            "created_at": timestamp,
                            "snippet": snippet,
                            "matched_line": matched_line,
                            "line_number": line_number,
                            "score": float(text.casefold().count(query_lower) if query_lower else 0)
                            + (0.25 if is_projection_candidate else 0.0),
                            "tags": [
                                "canonical=raw_events",
                                f"source={header['source_agent']}",
                            ],
                            "projection_candidate": is_projection_candidate,
                            "evidence_source": "raw_event_revision",
                        }
                    )
                serialized.sort(
                    key=lambda item: (
                        item["score"], item["created_at"], item["turn_number"]
                    ),
                    reverse=True,
                )
                serialized = serialized[: max(0, int(limit))]
                for item in serialized:
                    store.record_access(
                        item["revision_id"], "search", query=query, consumer="session_search"
                    )
                    store.record_access(
                        item["revision_id"], "result", query=query, consumer="session_search"
                    )
            finally:
                store.close()

            return {
                "success": True,
                "query": query or f"session={session_id}",
                "results": serialized,
                "count": len(serialized),
                "access_filter": access_summary,
                "evidence_source": "raw_event_revision",
            }
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            self._logger.error("会话搜索失败: %s", type(exc).__name__, exc_info=True)
            return {
                "success": False,
                "message": f"搜索失败: {exc}",
            }

    @staticmethod
    def _canonical_turn_text(turn: Dict[str, Any]) -> str:
        sections = [
            str(turn.get("user_content") or ""),
            str(turn.get("assistant_content") or ""),
            str(turn.get("reasoning") or ""),
        ]
        for key in ("tool_calls", "tool_results", "attachments", "raw_event_refs"):
            value = turn.get(key)
            if value:
                sections.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return "\n".join(section for section in sections if section)

    @staticmethod
    def _canonical_snippet(text: str, query_lower: str) -> tuple[str, str, int]:
        lines = text.splitlines() or [""]
        matched_index = 0
        if query_lower:
            matched_index = next(
                (i for i, line in enumerate(lines) if query_lower in line.casefold()),
                0,
            )
        matched_line = lines[matched_index]
        if len(matched_line) > 700:
            match_at = matched_line.casefold().find(query_lower) if query_lower else 0
            window_start = max(0, match_at - 320)
            window_end = min(len(matched_line), window_start + 700)
            window_start = max(0, window_end - 700)
            matched_line = (
                ("..." if window_start else "")
                + matched_line[window_start:window_end]
                + ("..." if window_end < len(lines[matched_index]) else "")
            )
        start = max(0, matched_index - 2)
        end = min(len(lines), matched_index + 3)
        snippet_lines = [line[:80] for line in lines[start:end]]
        snippet_lines[matched_index - start] = matched_line
        snippet = "\n".join(snippet_lines)
        if len(snippet) > 800:
            snippet = matched_line
        return snippet, matched_line, matched_index + 1

    def knowledge_ingest(
        self,
        content: str,
        tags: List[str] | None = None,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        """Write user-provided knowledge into the configured storage backend."""
        from core.privacy.ingestion_security import (
            assess_ingestion_security,
            attach_security_fields,
            merge_security_tags,
        )

        ingest_tags = list(tags or [])
        if "human" not in ingest_tags:
            ingest_tags.append("human")
        if "mnemos-ingest" not in ingest_tags:
            ingest_tags.append("mnemos-ingest")
        security = assess_ingestion_security(content)
        ingest_tags = merge_security_tags(ingest_tags, security)

        try:
            receipt = self._create_ingestion_receipt(
                content=content,
                source_agent=principal.agent,
                source_path="knowledge_ingest:human",
                title="knowledge-ingest",
                metadata={"tags": ingest_tags, "entrypoint": "knowledge_ingest"},
            )
            if not receipt.get("success"):
                return attach_security_fields(
                    {
                        "success": False,
                        "message": "canonical capture receipt failed; formal ingest blocked",
                        "tags": ingest_tags,
                        "ingested_length": len(content),
                        "pipeline": "blocked_before_storage_backend",
                        "quality_decision": "capture_failed_recoverable",
                        "ingestion_receipt": receipt,
                    },
                    security,
                )
            ingest_tags.extend(
                [
                    f"source_event_id={receipt.get('source_event_id', '')}",
                    f"raw_event_id={receipt.get('raw_event_id', '')}",
                ]
            )
            backend = self._storage_backend()
            results = backend.save(content=content, tags=ingest_tags, title="knowledge-ingest")
            if results:
                result = results[0]
                return attach_security_fields(
                    {
                        "success": True,
                        "message": "知识已成功摄入，将自动同步到 Wiki 并经过解析器处理",
                        "uid": result.uid,
                        "tags": result.tags,
                        "ingested_length": len(content),
                        "pipeline": (
                            "CaptureService receipt → StorageBackend → Wiki 00-Inbox → "
                            "Charon(语义索引/标签/热度) → 知识图谱"
                        ),
                        "ingestion_receipt": receipt,
                    },
                    security,
                )
            return attach_security_fields(
                {
                    "success": False,
                    "message": "摄入返回空结果",
                },
                security,
            )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            self._logger.error("知识摄入失败: %s", type(exc).__name__, exc_info=True)
            return attach_security_fields(
                {
                    "success": False,
                    "message": f"摄入失败: {exc}",
                },
                security,
            )

    def _create_ingestion_receipt(self, **kwargs) -> Dict[str, Any]:
        if self._receipt_factory is not None:
            return self._receipt_factory(**kwargs)
        from core.sync_framework.ingestion_receipt import create_ingestion_receipt

        return create_ingestion_receipt(**kwargs)
