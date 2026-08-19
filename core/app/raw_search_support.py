"""Query/result helpers shared by the canonical Raw index owner."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)

RAW_INDEX_GET_BY_PATH_ROW = 7
SNIPPET_CONTEXT_LINES = 3
SNIPPET_MAX_CHARS = 800


@dataclass
class RawSearchResult:
    """Structured Raw search result."""

    file_path: str
    session_id: str
    date: str
    snippet: str
    matched_line: str
    line_number: int
    score: float
    source: str
    turn_number: int
    tags: List[str]
    created_at: str
    scope: str = ""
    source_agent: str = ""
    project: str = ""
    acl_schema_version: int = 0
    acl_metadata_complete: bool = False
    acl_reconciliation_status: str = ""


class RawIndexSupportMixin:
    """Tag, access-ledger, snippet, and diagnostic behavior for ``RawIndex``."""

    raw_event_store: Any
    db_path: Path
    raw_dir: Path

    def _connect(self) -> sqlite3.Connection:
        raise NotImplementedError

    def _parse_frontmatter(self, text: str) -> Tuple[Dict[str, Any], str]:
        raise NotImplementedError

    def _normalize_required_tags(
        self, required_tags: List[str]
    ) -> Tuple[List[str], Dict[str, str]]:
        """Normalize ordinary tags and key/value requirements."""

        normalized: List[str] = []
        key_values: Dict[str, str] = {}
        for tag in required_tags:
            tag = tag.strip().lower()
            if not tag:
                continue
            if "=" in tag:
                key, val = tag.split("=", 1)
                key_values[key.strip()] = val.strip()
            else:
                normalized.append(tag)
        return normalized, key_values

    def _fetch_files_by_tags(
        self, cursor: sqlite3.Cursor, normalized: List[str]
    ) -> List[str]:
        """Return paths that contain every normalized ordinary tag."""

        placeholders = ",".join("?" * len(normalized))
        tag_subquery = " ".join(
            [
                "SELECT file_path FROM raw_tags",
                "WHERE tag IN (" + placeholders + ")",
                "GROUP BY file_path",
                "HAVING COUNT(DISTINCT tag) = ?",
            ]
        )
        params = normalized + [len(normalized)]
        cursor.execute(tag_subquery, params)
        return [row[0] for row in cursor.fetchall()]

    def _key_values_match(
        self,
        key_values: Dict[str, str],
        frontmatter: Dict[str, Any],
        tags: List[str],
    ) -> bool:
        """Check frontmatter and tag evidence for every key/value requirement."""

        fm_lower = {
            str(key).lower(): str(value).lower()
            for key, value in frontmatter.items()
        }
        tag_set = {str(tag).strip().lower() for tag in tags}
        return all(
            fm_lower.get(str(key).lower()) == str(value).lower()
            or (
                f"{str(key).strip().lower()}="
                f"{str(value).strip().lower()}"
            )
            in tag_set
            for key, value in key_values.items()
        )

    def _make_tag_result(
        self, row: Tuple, tags: List[str], content: str
    ) -> Dict[str, Any]:
        """Convert a Raw index row into the public tag-query projection."""

        return {
            "file_path": row[0],
            "session_id": row[1] or "",
            "date": row[2] or "",
            "created_at": row[3] or "",
            "source": row[4] or "",
            "tags": tags,
            "content": content,
        }

    def list_by_tags(
        self, required_tags: List[str], limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Query files that satisfy every tag requirement."""

        if not required_tags:
            return []
        normalized, key_values = self._normalize_required_tags(required_tags)
        if not normalized and not key_values:
            return []

        cursor = self._connect().cursor()
        if normalized:
            file_paths = self._fetch_files_by_tags(cursor, normalized)
            if not file_paths:
                return []
        else:
            cursor.execute("SELECT file_path FROM raw_index")
            file_paths = [row[0] for row in cursor.fetchall()]

        results: List[Dict[str, Any]] = []
        for rel_path in file_paths:
            row = cursor.execute(
                """SELECT file_path, session_id, date, created_at, source, tags, content
                   FROM raw_index WHERE file_path = ?""",
                (rel_path,),
            ).fetchone()
            if not row:
                continue
            content = row[6] or ""
            tags = json.loads(row[5]) if row[5] else []
            if key_values:
                frontmatter, _body = self._parse_frontmatter(content)
                if not self._key_values_match(key_values, frontmatter, tags):
                    continue
            results.append(self._make_tag_result(row, tags, content))
            if limit and len(results) >= limit:
                break
        return results

    def _record_raw_access_by_turn(
        self,
        *,
        source: str,
        session_id: str,
        turn_number: int,
        access_type: str,
        query: Optional[str] = None,
        consumer: str = "raw_index",
    ) -> bool:
        """Record Raw access, translating Obsidian's one-based turn if needed."""

        store = getattr(self, "raw_event_store", None)
        if store is None or not source or not session_id:
            return False
        candidates = [turn_number]
        if turn_number > 0:
            candidates = [turn_number - 1, turn_number]
        for candidate in candidates:
            try:
                if store.record_turn_access(
                    source_agent=source,
                    session_id=session_id,
                    turn_number=candidate,
                    access_type=access_type,
                    query=query,
                    consumer=consumer,
                ):
                    return True
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                logger.debug("[RawIndex] raw access record failed", exc_info=True)
                return False
        return False

    def record_result_access(
        self,
        result: RawSearchResult,
        access_type: str,
        *,
        query: Optional[str] = None,
        consumer: str = "raw_index",
    ) -> bool:
        """Record explicit result usage such as hit/reference/view."""

        return self._record_raw_access_by_turn(
            source=result.source,
            session_id=result.session_id,
            turn_number=result.turn_number,
            access_type=access_type,
            query=query,
            consumer=consumer,
        )

    def record_authorized_results(
        self,
        query: str,
        results: List[RawSearchResult],
    ) -> None:
        """Record search metrics only after the caller authorizes results."""

        for result in results:
            self._record_raw_access_by_turn(
                source=result.source,
                session_id=result.session_id,
                turn_number=result.turn_number,
                access_type="search",
                query=query,
            )
            self.record_result_access(result, "result", query=query)

    def _extract_snippet(
        self, content: str, query_lower: str, abs_path: str
    ) -> Tuple[str, str, int, float]:
        """Extract the best matching line and bounded surrounding context."""

        del abs_path
        if not content:
            return "", "", 0, 0.0
        lines = content.splitlines()
        best_idx = -1
        best_score = 0.0
        for index, line in enumerate(lines):
            line_lower = line.lower()
            if query_lower and query_lower in line_lower:
                length_score = 1.0 if 20 <= len(line) <= 200 else 0.7
                position_score = 1.0 - (index / max(len(lines), 1)) * 0.3
                score = length_score * position_score
                if score > best_score:
                    best_score = score
                    best_idx = index
        if best_idx < 0:
            snippet = "\n".join(lines[: SNIPPET_CONTEXT_LINES + 1])
            return snippet[:SNIPPET_MAX_CHARS], "", 1, 0.3
        start = max(0, best_idx - SNIPPET_CONTEXT_LINES)
        end = min(len(lines), best_idx + SNIPPET_CONTEXT_LINES + 1)
        snippet = "\n".join(lines[start:end])
        if len(snippet) > SNIPPET_MAX_CHARS:
            snippet = snippet[:SNIPPET_MAX_CHARS] + "\n..."
        return snippet, lines[best_idx], best_idx + 1, round(best_score, 3)

    def get_by_path(self, rel_path: str) -> Optional[Dict]:
        """Return one complete record by Raw-relative path."""

        cursor = self._connect().cursor()
        cursor.execute(
            """SELECT file_path, session_id, date, created_at, content,
                      turn_number, source, tags, abs_path
               FROM raw_index WHERE file_path = ?""",
            (rel_path,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "file_path": row[0],
            "session_id": row[1],
            "date": row[2],
            "created_at": row[3],
            "content": row[4],
            "turn_number": row[5],
            "source": row[6],
            "tags": (
                json.loads(row[RAW_INDEX_GET_BY_PATH_ROW])
                if row[RAW_INDEX_GET_BY_PATH_ROW]
                else []
            ),
            "abs_path": row[8],
        }

    def health_check(self) -> Dict[str, Any]:
        """Return the bounded Raw index health projection."""

        cursor = self._connect().cursor()
        cursor.execute("SELECT COUNT(*) FROM raw_index")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM raw_fts")
        fts_total = cursor.fetchone()[0]
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "status": "ok"
            if total > 0 or not self.raw_dir.exists()
            else "empty",
            "indexed_files": total,
            "fts_entries": fts_total,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "raw_dir": str(self.raw_dir),
            "db_path": str(self.db_path),
        }
