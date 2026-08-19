# -*- coding: utf-8 -*-
"""Durable checkpoints for chunked distillation.

The queue retry policy can schedule a failed task again, but it cannot make a
large distillation succeed by itself. This store keeps successfully extracted
chunk results so the next attempt resumes from the missing chunk instead of
repeating the whole session.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from core.hephaestus.distill_execution_spec import DistillExecutionSpec
from core.hephaestus.distill_input_spec import DistillInputSpec, OUTPUT_CONTRACT_VERSION
from core.hephaestus.distillation_contract import (
    canonical_extraction_output_hash,
    canonical_fragment_payload,
    validate_checkpoint_extraction_output,
)
from core.hephaestus.distillation_models import KnowledgeFragment

logger = logging.getLogger(__name__)
DISTILLATION_INPUT_CONTRACT_VERSION = "lossless-visible-v1"
CHECKPOINT_OUTPUT_SCHEMA_VERSION = "mnemos.distill_checkpoint_output.v1"


@dataclass(frozen=True)
class CheckpointAdmissionRequest:
    """Identity fields known before a cached extraction is loaded."""

    input_spec_hash: str
    output_contract_version: str = OUTPUT_CONTRACT_VERSION
    # A hash alone only proves identity.  Reuse must also re-run the canonical
    # union validator against the immutable input that supplied that hash.
    input_spec: DistillInputSpec | None = None

    @classmethod
    def for_input_spec(cls, input_spec: DistillInputSpec) -> "CheckpointAdmissionRequest":
        """Build a strict cache-read request from the immutable input owner."""
        return cls(input_spec_hash=input_spec.input_spec_hash, input_spec=input_spec)


@dataclass(frozen=True)
class CheckpointAdmission(CheckpointAdmissionRequest):
    """Admission proof persisted beside a completed chunk result."""

    canonical_output_hash: str = ""
    judgment: str = ""


def build_chunk_fingerprint(
    chunk: List[Dict[str, Any]],
    chunk_index: int,
    chunk_size: int,
    incremental_batch_turns: Any,
    execution_spec_hash: str,
) -> str:
    """Hash the exact extraction input and every contract field affecting output."""
    payload = {
        "schema": 3,
        "execution_spec_hash": execution_spec_hash,
        "chunk_index": chunk_index,
        "chunk_size": chunk_size,
        "incremental_batch_turns": incremental_batch_turns,
        "messages": chunk,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_chunk_info(
    chunk_index: int,
    chunk: List[Dict[str, Any]],
    chunk_meta: Dict[str, Any],
    chunk_fragments: List[KnowledgeFragment],
    message_positions: Dict[int, int],
    execution_spec: DistillExecutionSpec | None = None,
    checkpoint_miss_reason: str = "",
    checkpoint_spec_diff_fields: tuple[str, ...] = (),
    admission: CheckpointAdmission | None = None,
) -> Dict[str, Any]:
    """Build cache/provenance metadata for one extracted chunk."""
    covered_turns = [
        message.get("turn") or message_positions.get(id(message), chunk_index + 1)
        for message in chunk
    ]
    turn_range = f"{min(covered_turns)}-{max(covered_turns)}" if covered_turns else ""
    info: Dict[str, Any] = {
        "input_contract_version": DISTILLATION_INPUT_CONTRACT_VERSION,
        "chunk_index": chunk_index,
        "covered_turn_range": turn_range,
        "fragment_count": len(chunk_fragments) if chunk_fragments else 0,
        "truncated": chunk_meta.get("truncated", False),
        "omitted_turns": chunk_meta.get("omitted_turns", 0),
        "message_truncations": chunk_meta.get("message_truncations", []),
    }
    if execution_spec is not None:
        info.update(
            {
                "execution_spec_schema": execution_spec.schema_version,
                "execution_spec_hash": execution_spec.execution_spec_hash,
                "prompt_hash": execution_spec.prompt_hash,
                "schema_hash": execution_spec.output_schema_hash,
                "model_id": list(execution_spec.model_ids),
                "checkpoint_reused": False,
                "cache_hit": False,
                "miss_reason": checkpoint_miss_reason,
                "spec_diff_fields": list(checkpoint_spec_diff_fields),
            }
        )
    if admission is not None:
        info.update(
            {
                "input_spec_hash": admission.input_spec_hash,
                "output_contract_version": admission.output_contract_version,
                "canonical_output_hash": admission.canonical_output_hash,
                "output_judgment": admission.judgment,
            }
        )
    return info


@dataclass(frozen=True)
class ChunkCheckpointLookup:
    """Typed cache hit/miss with machine-readable provenance."""

    cache_hit: bool
    miss_reason: str = ""
    fragments: tuple[KnowledgeFragment, ...] = ()
    chunk_info: Dict[str, Any] | None = None
    structured_output: Dict[str, Any] | None = None
    canonical_output: Dict[str, Any] | None = None
    execution_spec_hash: str = ""
    stored_execution_spec_hash: str = ""
    spec_diff_fields: tuple[str, ...] = ()
    admission: CheckpointAdmission | None = None


def fragment_to_dict(fragment: KnowledgeFragment) -> Dict[str, Any]:
    """Serialize a KnowledgeFragment without relying on dataclass helpers."""
    return {
        "form": fragment.form,
        "title": fragment.title,
        "frontmatter": fragment.frontmatter,
        "background": fragment.background,
        "core_content": fragment.core_content,
        "boundaries": fragment.boundaries,
        "anti_patterns": fragment.anti_patterns,
        "related_concepts": fragment.related_concepts,
        "claim_ids": fragment.claim_ids,
        "relations": fragment.relations,
        "self_check_passed": fragment.self_check_passed,
        "self_check_issues": fragment.self_check_issues,
        "self_check_severity": fragment.self_check_severity,
        "cross_agent_links": fragment.cross_agent_links,
        "keywords": fragment.keywords,
        "ai_expansion": fragment.ai_expansion,
    }


def fragment_from_dict(data: Dict[str, Any]) -> KnowledgeFragment:
    """Restore a KnowledgeFragment saved by fragment_to_dict()."""
    return KnowledgeFragment(
        form=data.get("form", "未知"),
        title=data.get("title", "无标题"),
        frontmatter=data.get("frontmatter", {}) or {},
        background=data.get("background", "") or "",
        core_content=data.get("core_content", "") or "",
        boundaries=data.get("boundaries", {}) or {},
        anti_patterns=data.get("anti_patterns", []) or [],
        related_concepts=data.get("related_concepts", []) or [],
        claim_ids=data.get("claim_ids", []) or [],
        relations=data.get("relations", []) or [],
        self_check_passed=bool(data.get("self_check_passed", True)),
        self_check_issues=data.get("self_check_issues", []) or [],
        self_check_severity=data.get("self_check_severity", "ok") or "ok",
        cross_agent_links=data.get("cross_agent_links", []) or [],
        keywords=data.get("keywords", []) or [],
        ai_expansion=data.get("ai_expansion", "") or "",
    )


def build_checkpoint_output_hash(
    canonical_output: Mapping[str, Any],
) -> str:
    """Return the same root-output identity used by fresh extraction admission."""
    return canonical_extraction_output_hash(canonical_output=canonical_output)


def _serialized_fragment_hash(fragments: List[KnowledgeFragment]) -> str:
    """Bind parsed checkpoint fragments without changing the root admission hash."""
    payload = [fragment_to_dict(fragment) for fragment in fragments]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical_output_matches_fragments(
    canonical_output: Mapping[str, Any],
    fragments: List[KnowledgeFragment],
    structured_output: Mapping[str, Any] | None,
) -> bool:
    """Ensure separately stored parsed fragments still match the root payload."""
    root_fragments = canonical_output.get("fragments")
    if not isinstance(root_fragments, list):
        return False
    if root_fragments != [canonical_fragment_payload(fragment) for fragment in fragments]:
        return False
    return canonical_output.get("structured_output") == dict(structured_output or {})


def _admission_from_chunk_info(
    chunk_info: Dict[str, Any],
) -> CheckpointAdmission | None:
    input_spec_hash = str(chunk_info.get("input_spec_hash") or "")
    output_contract_version = str(chunk_info.get("output_contract_version") or "")
    canonical_output_hash = str(chunk_info.get("canonical_output_hash") or "")
    judgment = str(chunk_info.get("output_judgment") or "")
    if not all((input_spec_hash, output_contract_version, canonical_output_hash, judgment)):
        return None
    return CheckpointAdmission(
        input_spec_hash=input_spec_hash,
        output_contract_version=output_contract_version,
        canonical_output_hash=canonical_output_hash,
        judgment=judgment,
    )


class ChunkCheckpointStore:
    """SQLite-backed per-chunk checkpoint storage."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser()
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _create_current_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS distill_chunk_results (
                session_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_hash TEXT NOT NULL,
                execution_spec_hash TEXT NOT NULL,
                execution_spec_json TEXT NOT NULL,
                status TEXT NOT NULL,
                fragment_json TEXT NOT NULL DEFAULT '[]',
                chunk_info_json TEXT NOT NULL DEFAULT '{}',
                structured_output_json TEXT,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, chunk_index, chunk_hash)
            )
            """
        )

    @staticmethod
    def _schema_is_current(conn: sqlite3.Connection) -> bool:
        rows = conn.execute("PRAGMA table_info(distill_chunk_results)").fetchall()
        if not rows:
            return False
        columns = {str(row[1]) for row in rows}
        primary_key = [
            str(row[1])
            for row in sorted(rows, key=lambda item: int(item[5] or 0))
            if int(row[5] or 0) > 0
        ]
        return {
            "execution_spec_hash",
            "execution_spec_json",
        }.issubset(columns) and primary_key == ["session_id", "chunk_index", "chunk_hash"]

    @classmethod
    def _migrate_v1_schema(cls, conn: sqlite3.Connection) -> None:
        """Transactionally rebuild v1 rows as non-reusable historical generations."""
        conn.execute("DROP TABLE IF EXISTS distill_chunk_results_v1_backup")
        conn.execute(
            "ALTER TABLE distill_chunk_results RENAME TO distill_chunk_results_v1_backup"
        )
        cls._create_current_table(conn)
        conn.execute(
            """
            INSERT INTO distill_chunk_results (
                session_id, chunk_index, chunk_hash,
                execution_spec_hash, execution_spec_json,
                status, fragment_json, chunk_info_json,
                structured_output_json, error, created_at, updated_at
            )
            SELECT
                session_id, chunk_index, chunk_hash,
                '', '{}',
                status, fragment_json, chunk_info_json,
                structured_output_json, error, created_at, updated_at
            FROM distill_chunk_results_v1_backup
            """
        )
        conn.execute("DROP TABLE distill_chunk_results_v1_backup")

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='distill_chunk_results'"
            ).fetchone()
            if exists is None:
                self._create_current_table(conn)
            elif not self._schema_is_current(conn):
                self._migrate_v1_schema(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_distill_chunk_results_status
                ON distill_chunk_results(status, updated_at)
                """
            )
            # v4 stores the complete admitted root payload in a typed envelope
            # inside structured_output_json.  Earlier rows can be retained for
            # diagnostics but are never reusable because that root is absent.
            conn.execute("PRAGMA user_version = 4")
        self._schema_ready = True

    def migrate_schema(self) -> bool:
        """Ensure the execution-spec schema and report whether it changed."""
        with self._connect() as conn:
            was_current = self._schema_is_current(conn)
        self._schema_ready = False
        self._ensure_schema()
        return not was_current

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _parse_fragment_list(raw: str) -> Optional[List[KnowledgeFragment]]:
        data = json.loads(raw or "[]")
        if not isinstance(data, list):
            return None

        fragments: List[KnowledgeFragment] = []
        for item in data:
            if not isinstance(item, dict):
                return None
            fragments.append(fragment_from_dict(item))
        return fragments

    @staticmethod
    def _parse_chunk_info(raw: str) -> Optional[Dict[str, Any]]:
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            return None
        return data

    @staticmethod
    def _parse_checkpoint_output(
        raw: str | None,
    ) -> Optional[tuple[Dict[str, Any], Dict[str, Any], str]]:
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        if data.get("schema_version") != CHECKPOINT_OUTPUT_SCHEMA_VERSION:
            return None
        canonical_output = data.get("canonical_output")
        if not isinstance(canonical_output, dict):
            return None
        structured_output = canonical_output.get("structured_output")
        if not isinstance(structured_output, dict):
            return None
        serialized_fragment_hash = str(data.get("serialized_fragment_hash") or "")
        if not serialized_fragment_hash:
            return None
        return dict(canonical_output), dict(structured_output), serialized_fragment_hash

    @staticmethod
    def _miss(
        reason: str,
        spec: DistillExecutionSpec,
        *,
        stored_hash: str = "",
        diff_fields: tuple[str, ...] = (),
    ) -> ChunkCheckpointLookup:
        return ChunkCheckpointLookup(
            cache_hit=False,
            miss_reason=reason,
            execution_spec_hash=spec.execution_spec_hash,
            stored_execution_spec_hash=stored_hash,
            spec_diff_fields=diff_fields,
        )

    def _decode_completed_row(
        self,
        row: sqlite3.Row,
        spec: DistillExecutionSpec,
        admission_request: CheckpointAdmissionRequest | None,
    ) -> ChunkCheckpointLookup:
        stored_hash = str(row["execution_spec_hash"] or "")
        raw_spec = str(row["execution_spec_json"] or "")
        if not stored_hash or raw_spec in {"", "{}"}:
            return self._miss(
                "legacy_execution_spec_missing", spec, stored_hash=stored_hash
            )
        try:
            stored_spec = DistillExecutionSpec.from_json(raw_spec)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return self._miss("corrupt_execution_spec", spec, stored_hash=stored_hash)
        if stored_hash != stored_spec.execution_spec_hash:
            return self._miss("corrupt_execution_spec", spec, stored_hash=stored_hash)
        if stored_hash != spec.execution_spec_hash:
            return self._miss(
                "execution_spec_changed",
                spec,
                stored_hash=stored_hash,
                diff_fields=spec.diff_fields(stored_spec),
            )
        if str(row["status"]) != "completed":
            return self._miss(
                "checkpoint_not_completed", spec, stored_hash=stored_hash
            )
        try:
            fragments = self._parse_fragment_list(row["fragment_json"])
            chunk_info = self._parse_chunk_info(row["chunk_info_json"])
            parsed_output = self._parse_checkpoint_output(row["structured_output_json"])
        except (json.JSONDecodeError, TypeError, ValueError):
            return self._miss("corrupt_checkpoint_payload", spec, stored_hash=stored_hash)
        if fragments is None or chunk_info is None:
            return self._miss("corrupt_checkpoint_payload", spec, stored_hash=stored_hash)
        if parsed_output is None:
            return self._miss("legacy_root_output_missing", spec, stored_hash=stored_hash)
        canonical_output, structured_output, serialized_fragment_hash = parsed_output
        if not _canonical_output_matches_fragments(
            canonical_output, fragments, structured_output
        ):
            return self._miss("corrupt_root_output_binding", spec, stored_hash=stored_hash)
        if serialized_fragment_hash != _serialized_fragment_hash(fragments):
            return self._miss("corrupt_fragment_serialization", spec, stored_hash=stored_hash)
        admission = _admission_from_chunk_info(chunk_info)
        if admission is None:
            return self._miss(
                "legacy_output_admission_missing", spec, stored_hash=stored_hash
            )
        if admission_request is None or not isinstance(
            admission_request.input_spec, DistillInputSpec
        ):
            return self._miss(
                "checkpoint_input_spec_missing", spec, stored_hash=stored_hash
            )
        if admission_request is not None:
            if admission.input_spec_hash != admission_request.input_spec_hash:
                return self._miss(
                    "checkpoint_input_spec_changed", spec, stored_hash=stored_hash
                )
            if admission.output_contract_version != admission_request.output_contract_version:
                return self._miss(
                    "checkpoint_output_contract_changed", spec, stored_hash=stored_hash
                )
            if admission_request.input_spec.input_spec_hash != admission.input_spec_hash:
                return self._miss(
                    "checkpoint_input_spec_changed", spec, stored_hash=stored_hash
                )
        if admission.canonical_output_hash != build_checkpoint_output_hash(
            canonical_output,
        ):
            return self._miss(
                "corrupt_output_admission", spec, stored_hash=stored_hash
            )
        validation = validate_checkpoint_extraction_output(
            canonical_output=canonical_output,
            input_spec=admission_request.input_spec,
        )
        if not validation.valid:
            return self._miss(
                "checkpoint_output_contract_invalid", spec, stored_hash=stored_hash
            )
        if validation.output_judgment != admission.judgment:
            return self._miss(
                "checkpoint_output_judgment_mismatch", spec, stored_hash=stored_hash
            )
        return ChunkCheckpointLookup(
            cache_hit=True,
            fragments=tuple(fragments),
            chunk_info=chunk_info,
            structured_output=structured_output,
            canonical_output=canonical_output,
            execution_spec_hash=spec.execution_spec_hash,
            stored_execution_spec_hash=stored_hash,
            admission=admission,
        )

    def lookup_completed(
        self,
        session_id: str,
        chunk_index: int,
        chunk_hash: str,
        execution_spec: DistillExecutionSpec,
        admission_request: CheckpointAdmissionRequest | None = None,
    ) -> ChunkCheckpointLookup:
        """Return an exact hit or an auditable reason the checkpoint is unusable."""
        try:
            self._ensure_schema()
            with self._connect() as conn:
                exact = conn.execute(
                    """
                    SELECT * FROM distill_chunk_results
                    WHERE session_id = ? AND chunk_index = ? AND chunk_hash = ?
                    """,
                    (session_id, chunk_index, chunk_hash),
                ).fetchone()
                if exact is not None:
                    return self._decode_completed_row(
                        exact, execution_spec, admission_request
                    )
                previous = conn.execute(
                    """
                    SELECT * FROM distill_chunk_results
                    WHERE session_id = ? AND chunk_index = ?
                    ORDER BY updated_at DESC, chunk_hash DESC
                    LIMIT 1
                    """,
                    (session_id, chunk_index),
                ).fetchone()
            if previous is None:
                return self._miss("checkpoint_not_found", execution_spec)
            stored_hash = str(previous["execution_spec_hash"] or "")
            raw_spec = str(previous["execution_spec_json"] or "")
            if not stored_hash or raw_spec in {"", "{}"}:
                return self._miss(
                    "legacy_execution_spec_missing",
                    execution_spec,
                    stored_hash=stored_hash,
                )
            try:
                previous_spec = DistillExecutionSpec.from_json(raw_spec)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                return self._miss(
                    "corrupt_execution_spec", execution_spec, stored_hash=stored_hash
                )
            if previous_spec.execution_spec_hash != stored_hash:
                return self._miss(
                    "corrupt_execution_spec", execution_spec, stored_hash=stored_hash
                )
            if stored_hash != execution_spec.execution_spec_hash:
                return self._miss(
                    "execution_spec_changed",
                    execution_spec,
                    stored_hash=stored_hash,
                    diff_fields=execution_spec.diff_fields(previous_spec),
                )
            return self._miss(
                "chunk_input_changed", execution_spec, stored_hash=stored_hash
            )
        except (OSError, sqlite3.Error, TypeError, ValueError, AttributeError):
            logger.warning(
                "[Distillation] chunk checkpoint read failed for %s[%s]",
                session_id,
                chunk_index,
                exc_info=True,
            )
            return self._miss("checkpoint_read_error", execution_spec)

    def load_completed(
        self,
        session_id: str,
        chunk_index: int,
        chunk_hash: str,
        execution_spec: DistillExecutionSpec,
        admission_request: CheckpointAdmissionRequest | None = None,
    ) -> Optional[Tuple[List[KnowledgeFragment], Dict[str, Any], Optional[Dict[str, Any]]]]:
        """Compatibility-shaped return over the strict execution-spec lookup."""
        lookup = self.lookup_completed(
            session_id,
            chunk_index,
            chunk_hash,
            execution_spec,
            admission_request,
        )
        if not lookup.cache_hit or lookup.chunk_info is None:
            return None
        return list(lookup.fragments), lookup.chunk_info, lookup.structured_output

    def save_completed(
        self,
        session_id: str,
        chunk_index: int,
        chunk_hash: str,
        execution_spec: DistillExecutionSpec,
        fragments: List[KnowledgeFragment],
        chunk_info: Dict[str, Any],
        structured_output: Optional[Dict[str, Any]],
        admission: CheckpointAdmission,
        *,
        canonical_output: Mapping[str, Any] | None,
        input_spec: DistillInputSpec,
    ) -> None:
        """Persist a successfully extracted chunk."""
        try:
            if not isinstance(admission, CheckpointAdmission):
                raise ValueError("completed checkpoint requires typed output admission")
            if admission.output_contract_version != OUTPUT_CONTRACT_VERSION:
                raise ValueError("unsupported checkpoint output contract")
            if not isinstance(input_spec, DistillInputSpec):
                raise ValueError("completed checkpoint requires immutable input spec")
            if execution_spec.input_spec_hash != input_spec.input_spec_hash:
                raise ValueError("checkpoint execution spec input identity mismatch")
            if admission.input_spec_hash != input_spec.input_spec_hash:
                raise ValueError("checkpoint admission input identity mismatch")
            if (
                execution_spec.output_admission_contract_version
                != admission.output_contract_version
            ):
                raise ValueError("checkpoint execution/output contract version mismatch")
            if not isinstance(canonical_output, Mapping):
                raise ValueError("completed checkpoint requires canonical root output")
            normalized_output = dict(canonical_output)
            validation = validate_checkpoint_extraction_output(
                canonical_output=normalized_output,
                input_spec=input_spec,
            )
            if not validation.valid:
                raise ValueError(
                    "completed checkpoint root violates output contract: "
                    + validation.error_text
                )
            if validation.output_judgment != admission.judgment:
                raise ValueError("checkpoint validation judgment mismatch")
            if normalized_output.get("judgment") != admission.judgment:
                raise ValueError("checkpoint root judgment mismatch")
            if not _canonical_output_matches_fragments(
                normalized_output, fragments, structured_output
            ):
                raise ValueError("checkpoint root output does not match parsed fragments")
            if admission.canonical_output_hash != build_checkpoint_output_hash(
                normalized_output,
            ):
                raise ValueError("checkpoint output admission hash mismatch")
            if (
                str(chunk_info.get("input_spec_hash") or "") != admission.input_spec_hash
                or str(chunk_info.get("output_contract_version") or "")
                != admission.output_contract_version
                or str(chunk_info.get("canonical_output_hash") or "")
                != admission.canonical_output_hash
                or str(chunk_info.get("output_judgment") or "") != admission.judgment
            ):
                raise ValueError("checkpoint output admission metadata mismatch")
            self._ensure_schema()
            now = self._now()
            checkpoint_output_json = json.dumps(
                {
                    "schema_version": CHECKPOINT_OUTPUT_SCHEMA_VERSION,
                    "canonical_output": normalized_output,
                    "serialized_fragment_hash": _serialized_fragment_hash(fragments),
                },
                ensure_ascii=False,
            )
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO distill_chunk_results (
                        session_id, chunk_index, chunk_hash,
                        execution_spec_hash, execution_spec_json, status,
                        fragment_json, chunk_info_json, structured_output_json,
                        error, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, '', ?, ?)
                    ON CONFLICT(session_id, chunk_index, chunk_hash) DO UPDATE SET
                        execution_spec_hash = excluded.execution_spec_hash,
                        execution_spec_json = excluded.execution_spec_json,
                        status = 'completed',
                        fragment_json = excluded.fragment_json,
                        chunk_info_json = excluded.chunk_info_json,
                        structured_output_json = excluded.structured_output_json,
                        error = '',
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        chunk_index,
                        chunk_hash,
                        execution_spec.execution_spec_hash,
                        execution_spec.canonical_json(),
                        json.dumps(
                            [fragment_to_dict(fragment) for fragment in fragments],
                            ensure_ascii=False,
                        ),
                        json.dumps(chunk_info, ensure_ascii=False),
                        checkpoint_output_json,
                        now,
                        now,
                    ),
                )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            logger.warning(
                "[Distillation] chunk checkpoint write failed for %s[%s]",
                session_id,
                chunk_index,
                exc_info=True,
            )

    def mark_failed(
        self,
        session_id: str,
        chunk_index: int,
        chunk_hash: str,
        execution_spec: DistillExecutionSpec,
        error: str,
    ) -> None:
        """Record the failed chunk for diagnostics; retries still ignore it."""
        try:
            self._ensure_schema()
            now = self._now()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO distill_chunk_results (
                        session_id, chunk_index, chunk_hash,
                        execution_spec_hash, execution_spec_json, status,
                        fragment_json, chunk_info_json, structured_output_json,
                        error, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'failed', '[]', '{}', NULL, ?, ?, ?)
                    ON CONFLICT(session_id, chunk_index, chunk_hash) DO UPDATE SET
                        execution_spec_hash = excluded.execution_spec_hash,
                        execution_spec_json = excluded.execution_spec_json,
                        status = 'failed',
                        error = excluded.error,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        chunk_index,
                        chunk_hash,
                        execution_spec.execution_spec_hash,
                        execution_spec.canonical_json(),
                        error[:2000],
                        now,
                        now,
                    ),
                )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            logger.warning(
                "[Distillation] chunk checkpoint failure mark failed for %s[%s]",
                session_id,
                chunk_index,
                exc_info=True,
            )

    def cleanup_older_than(self, days: int, dry_run: bool = False) -> int:
        """Delete stale completed/failed chunk checkpoints."""
        if days <= 0:
            return 0
        try:
            self._ensure_schema()
            cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            with self._connect() as conn:
                if dry_run:
                    row = conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM distill_chunk_results
                        WHERE status IN ('completed', 'failed') AND updated_at < ?
                        """,
                        (cutoff_iso,),
                    ).fetchone()
                    return int(row[0] if row else 0)
                cur = conn.execute(
                    """
                    DELETE FROM distill_chunk_results
                    WHERE status IN ('completed', 'failed') AND updated_at < ?
                    """,
                    (cutoff_iso,),
                )
                return int(cur.rowcount or 0)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            logger.warning(
                "[Distillation] chunk checkpoint cleanup failed",
                exc_info=True,
            )
            return -1
