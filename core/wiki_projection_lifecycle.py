"""Durable Wiki page identity, mutation, and projection receipt lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from unittest.mock import Mock

from core.cognitive.state_contract import sha256_json
from core.config import get_config
from core.db_utils import render_sql

MUTATION_TYPES = frozenset({"create", "update", "move", "delete"})
TERMINAL_RECEIPT_OUTCOMES = frozenset({"ack", "noop"})
DEFAULT_REQUIRED_CONSUMERS = (
    "knowledge_graph",
    "cognitive_graph",
    "relation_embeddings",
    "wiki_search_index",
    "wiki_metrics",
    "moc_navigation",
)
WIKI_SUBJECT_DELETION_SCHEMA_VERSION = "mnemos.wiki_subject_deletion.v1"
WIKI_SUBJECT_DELETION_TABLE = "wiki_subject_deletion_receipts"
_WIKI_SUBJECT_DELETION_STATUSES = frozenset({"planned", "proposed", "tombstoned", "applied"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _subject_deletion_sql(template: str) -> str:
    return render_sql(
        template,
        identifiers={"subject_deletion_table": WIKI_SUBJECT_DELETION_TABLE},
    )


def _normalized_path(path: Path | str) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def _content_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _declared_content_sha256(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.split(":", 1)[1]
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("expected_content_sha256 must be an exact SHA-256 digest")
    return normalized


def _is_vault_content_path(path: Path | str, vault_dir: Path) -> bool:
    """Return whether ``path`` is a user-visible Markdown page in ``vault_dir``."""

    candidate = Path(path).expanduser().resolve(strict=False)
    try:
        relative = candidate.relative_to(vault_dir)
    except ValueError:
        return False
    return relative.suffix.lower() == ".md" and not any(
        part.startswith(".") for part in relative.parts
    )


def resolve_wiki_projection_db_path(config: Any | None = None) -> Path:
    """Resolve the one authoritative ledger path from the configured DB root."""

    cfg = config or get_config()
    for name in ("database_dir", "mnemos_dir", "data_dir"):
        value = getattr(cfg, name, None)
        if isinstance(value, Mock) or not isinstance(value, (str, os.PathLike, Path)):
            continue
        return Path(value).expanduser() / "wiki_projection.db"
    return Path.home() / ".mnemos" / "wiki_projection.db"


def _default_db_path() -> Path:
    """Compatibility seam retained for isolated tests and older callers."""

    return resolve_wiki_projection_db_path()


@dataclass(frozen=True)
class WikiMutationReceipt:
    """Immutable receipt for one causally ordered Wiki page mutation."""

    mutation_id: str
    page_id: str
    page_revision: str
    parent_revision: str
    sequence_no: int
    mutation_type: str
    page_path: str
    previous_path: str
    content_sha256: str
    tombstone: bool
    event_trace_id: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mutation receipt."""

        return asdict(self)


class WikiProjectionLedger:
    """Append-only page mutations plus per-consumer projection receipts.

    ``wiki_pages`` is only the current pointer. ``wiki_mutations`` remains the
    authoritative append-only history and makes rename/delete recovery possible.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS wiki_pages (
        page_id TEXT PRIMARY KEY,
        current_path TEXT NOT NULL UNIQUE,
        current_revision TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('active', 'tombstone')),
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_wiki_pages_state ON wiki_pages(lifecycle_state);
    CREATE INDEX IF NOT EXISTS idx_wiki_pages_content ON wiki_pages(content_sha256);

    CREATE TABLE IF NOT EXISTS wiki_mutations (
        mutation_id TEXT PRIMARY KEY,
        page_id TEXT NOT NULL,
        page_revision TEXT NOT NULL,
        parent_revision TEXT NOT NULL DEFAULT '',
        sequence_no INTEGER NOT NULL DEFAULT 0,
        mutation_type TEXT NOT NULL CHECK(mutation_type IN ('create', 'update', 'move', 'delete')),
        page_path TEXT NOT NULL,
        previous_path TEXT NOT NULL DEFAULT '',
        content_sha256 TEXT NOT NULL,
        tombstone INTEGER NOT NULL DEFAULT 0,
        event_trace_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(page_id, page_revision),
        FOREIGN KEY(page_id) REFERENCES wiki_pages(page_id)
    );
    CREATE INDEX IF NOT EXISTS idx_wiki_mutations_page ON wiki_mutations(page_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_wiki_mutations_event ON wiki_mutations(event_trace_id);

    CREATE TABLE IF NOT EXISTS projection_receipts (
        mutation_id TEXT NOT NULL,
        page_id TEXT NOT NULL,
        page_revision TEXT NOT NULL,
        consumer TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN ('ack', 'noop', 'retry', 'dead')),
        reason TEXT NOT NULL DEFAULT '',
        event_trace_id TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL,
        PRIMARY KEY(mutation_id, consumer),
        FOREIGN KEY(mutation_id) REFERENCES wiki_mutations(mutation_id)
    );
    CREATE INDEX IF NOT EXISTS idx_projection_receipts_outcome
        ON projection_receipts(outcome, consumer);

    CREATE TABLE IF NOT EXISTS wiki_projection_material_effects (
        effect_id TEXT PRIMARY KEY,
        command_id TEXT NOT NULL UNIQUE,
        source_id TEXT NOT NULL,
        consumer TEXT NOT NULL,
        target_ref TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        before_hash TEXT NOT NULL,
        after_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK(
            status IN ('executing', 'retryable', 'committed', 'dead_letter')
        ),
        reason_code TEXT NOT NULL DEFAULT '',
        outcome_json TEXT NOT NULL,
        outcome_hash TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 1 CHECK(attempt_count > 0),
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_wiki_projection_material_status
        ON wiki_projection_material_effects(status, consumer);

    CREATE TABLE IF NOT EXISTS wiki_subject_deletion_receipts (
        receipt_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        request_id TEXT NOT NULL,
        scope_kind TEXT NOT NULL,
        scope_value_hash TEXT NOT NULL,
        page_id TEXT NOT NULL,
        page_path TEXT NOT NULL,
        before_content_sha256 TEXT NOT NULL,
        mutation_id TEXT NOT NULL DEFAULT '',
        trusted_proposal_id TEXT NOT NULL DEFAULT '',
        event_trace_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK(status IN ('planned', 'proposed', 'tombstoned', 'applied')),
        created_at TEXT NOT NULL,
        tombstoned_at TEXT NOT NULL DEFAULT '',
        applied_at TEXT NOT NULL DEFAULT '',
        UNIQUE(request_id, page_id),
        FOREIGN KEY(page_id) REFERENCES wiki_pages(page_id)
    );
    CREATE INDEX IF NOT EXISTS idx_wiki_subject_deletion_request
        ON wiki_subject_deletion_receipts(request_id, status);
    CREATE INDEX IF NOT EXISTS idx_wiki_subject_deletion_path
        ON wiki_subject_deletion_receipts(page_path, status);
    """

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path).expanduser() if db_path else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(self.SCHEMA)
            self._migrate_schema(conn)
            conn.commit()

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(wiki_mutations)").fetchall()
        }
        if "parent_revision" not in columns:
            conn.execute(
                "ALTER TABLE wiki_mutations ADD COLUMN parent_revision TEXT NOT NULL DEFAULT ''"
            )
        if "sequence_no" not in columns:
            conn.execute(
                "ALTER TABLE wiki_mutations ADD COLUMN sequence_no INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute("UPDATE wiki_mutations SET sequence_no=rowid WHERE sequence_no=0")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_mutations_sequence "
            "ON wiki_mutations(sequence_no)"
        )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def tombstone_state(
        db_path: Path | str,
        page_path: Path | str,
    ) -> bool | None:
        """Return a page tombstone state without initializing a read path.

        ``None`` means an existing lifecycle database could not prove the
        state.  Callers with a body-read boundary must treat that as denied,
        rather than silently falling back to the Markdown file.
        """

        normalized = _normalized_path(page_path)
        return WikiProjectionLedger.tombstone_states(db_path, (page_path,)).get(normalized)

    @staticmethod
    def tombstone_states(
        db_path: Path | str,
        page_paths: Iterable[Path | str],
    ) -> dict[str, bool | None]:
        """Resolve many lifecycle headers from one read-only SQLite snapshot.

        A missing lifecycle database means no page has entered that lifecycle
        owner yet.  An existing but unreadable or invalid ledger returns
        ``None`` for every requested path so body readers fail closed.
        """

        normalized_paths = tuple(
            dict.fromkeys(_normalized_path(page_path) for page_path in page_paths)
        )
        if not normalized_paths:
            return {}
        database = Path(db_path).expanduser()
        if not database.is_file():
            return {path: False for path in normalized_paths}
        states: dict[str, bool | None] = {path: False for path in normalized_paths}
        try:
            with sqlite3.connect(
                database.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=5,
            ) as conn:
                conn.execute("PRAGMA query_only = ON")
                conn.execute("BEGIN")
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wiki_pages'"
                ).fetchone()
                if table is None:
                    return {path: None for path in normalized_paths}
                for offset in range(0, len(normalized_paths), 500):
                    batch = normalized_paths[offset : offset + 500]
                    query = render_sql(
                        "SELECT current_path, lifecycle_state FROM wiki_pages "
                        "WHERE current_path IN ({placeholders})",
                        placeholder_counts={"placeholders": len(batch)},
                    )
                    rows = conn.execute(
                        query,
                        batch,
                    ).fetchall()
                    for current_path, lifecycle_state in rows:
                        states[str(current_path)] = str(lifecycle_state) == "tombstone"
        except (OSError, sqlite3.Error, ValueError):
            return {path: None for path in normalized_paths}
        return states

    @staticmethod
    def _receipt(row: sqlite3.Row) -> WikiMutationReceipt:
        return WikiMutationReceipt(
            mutation_id=row["mutation_id"],
            page_id=row["page_id"],
            page_revision=row["page_revision"],
            parent_revision=row["parent_revision"] or "",
            sequence_no=int(row["sequence_no"] or 0),
            mutation_type=row["mutation_type"],
            page_path=row["page_path"],
            previous_path=row["previous_path"] or "",
            content_sha256=row["content_sha256"],
            tombstone=bool(row["tombstone"]),
            event_trace_id=row["event_trace_id"] or "",
            created_at=row["created_at"],
        )

    @staticmethod
    def _subject_deletion_receipt(row: sqlite3.Row) -> dict[str, Any]:
        """Expose a typed, content-free deletion receipt payload."""

        return {
            "receipt_id": str(row["receipt_id"]),
            "schema_version": str(row["schema_version"]),
            "request_id": str(row["request_id"]),
            "scope_kind": str(row["scope_kind"]),
            "scope_value_hash": str(row["scope_value_hash"]),
            "page_id": str(row["page_id"]),
            "page_path": str(row["page_path"]),
            "before_content_sha256": str(row["before_content_sha256"]),
            "mutation_id": str(row["mutation_id"] or ""),
            "trusted_proposal_id": str(row["trusted_proposal_id"] or ""),
            "event_trace_id": str(row["event_trace_id"] or ""),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "tombstoned_at": str(row["tombstoned_at"] or ""),
            "applied_at": str(row["applied_at"] or ""),
        }

    def page_identity(self, page_path: Path | str) -> dict[str, Any] | None:
        """Return one lifecycle page pointer without reading its Markdown body."""

        normalized = _normalized_path(page_path)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM wiki_pages WHERE current_path=?",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        return {
            "page_id": str(row["page_id"]),
            "current_path": str(row["current_path"]),
            "current_revision": str(row["current_revision"]),
            "content_sha256": str(row["content_sha256"]),
            "lifecycle_state": str(row["lifecycle_state"]),
            "updated_at": str(row["updated_at"]),
        }

    def subject_deletion_schema_present(self) -> bool:
        """Return whether this lifecycle owner has its typed delete receipt table."""

        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (WIKI_SUBJECT_DELETION_TABLE,),
            ).fetchone()
        return row is not None

    def prepare_subject_deletion(
        self,
        *,
        request_id: str,
        scope_kind: str,
        scope_value_hash: str,
        page_path: Path | str,
    ) -> dict[str, Any]:
        """Append the privacy receipt that must precede a Wiki body deletion.

        The receipt is deliberately independent of the physical file operation
        so a crash cannot leave an unrecorded deletion.  It contains only
        opaque request/scope identities and the existing content hash.
        """

        if not str(request_id).strip() or not str(scope_kind).strip():
            raise ValueError("subject deletion request_id and scope_kind are required")
        if not str(scope_value_hash).startswith("sha256:"):
            raise ValueError("subject deletion scope_value_hash must be a sha256 digest")
        normalized = _normalized_path(page_path)
        with self._conn() as conn:
            page = conn.execute(
                "SELECT * FROM wiki_pages WHERE current_path=?",
                (normalized,),
            ).fetchone()
            if page is None:
                raise LookupError(f"Wiki page is not registered in lifecycle ledger: {normalized}")
            existing = conn.execute(
                _subject_deletion_sql("""
                SELECT * FROM {subject_deletion_table}
                WHERE request_id=? AND page_id=?
                """),
                (str(request_id), str(page["page_id"])),
            ).fetchone()
            if existing is not None:
                immutable = (
                    WIKI_SUBJECT_DELETION_SCHEMA_VERSION,
                    str(request_id),
                    str(scope_kind),
                    str(scope_value_hash),
                    str(page["page_id"]),
                    normalized,
                    str(page["content_sha256"]),
                )
                actual = tuple(
                    existing[name]
                    for name in (
                        "schema_version",
                        "request_id",
                        "scope_kind",
                        "scope_value_hash",
                        "page_id",
                        "page_path",
                        "before_content_sha256",
                    )
                )
                if actual != immutable:
                    raise ValueError("wiki subject deletion receipt is immutable")
                return self._subject_deletion_receipt(existing)
            if str(page["lifecycle_state"]) != "active":
                raise RuntimeError(
                    "cannot create a new subject deletion receipt for a tombstoned Wiki page"
                )
            receipt_id = (
                "wiki-delete-"
                + hashlib.sha256(
                    f"{request_id}|{page['page_id']}|{scope_value_hash}".encode("utf-8")
                ).hexdigest()[:40]
            )
            now = _now()
            conn.execute(
                _subject_deletion_sql("""
                INSERT INTO {subject_deletion_table} (
                    receipt_id, schema_version, request_id, scope_kind,
                    scope_value_hash, page_id, page_path, before_content_sha256,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?)
                """),
                (
                    receipt_id,
                    WIKI_SUBJECT_DELETION_SCHEMA_VERSION,
                    str(request_id),
                    str(scope_kind),
                    str(scope_value_hash),
                    str(page["page_id"]),
                    normalized,
                    str(page["content_sha256"]),
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                _subject_deletion_sql("SELECT * FROM {subject_deletion_table} WHERE receipt_id=?"),
                (receipt_id,),
            ).fetchone()
            assert row is not None
            return self._subject_deletion_receipt(row)

    def mark_subject_deletion_proposed(
        self,
        receipt_id: str,
        proposal_id: str,
    ) -> dict[str, Any]:
        """Persist an enforce-mode proposal without pretending the page changed."""

        with self._conn() as conn:
            row = conn.execute(
                _subject_deletion_sql("SELECT * FROM {subject_deletion_table} WHERE receipt_id=?"),
                (str(receipt_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown Wiki subject deletion receipt: {receipt_id}")
            if str(row["status"]) == "applied":
                return self._subject_deletion_receipt(row)
            current_status = str(row["status"])
            if current_status not in {"planned", "proposed", "tombstoned"}:
                raise RuntimeError("cannot propose a completed Wiki deletion")
            existing = str(row["trusted_proposal_id"] or "")
            if existing and existing != str(proposal_id or ""):
                raise ValueError("trusted deletion proposal is immutable")
            conn.execute(
                _subject_deletion_sql("""
                UPDATE {subject_deletion_table}
                SET status=?, trusted_proposal_id=?
                WHERE receipt_id=?
                """),
                (
                    "tombstoned" if current_status == "tombstoned" else "proposed",
                    str(proposal_id or ""),
                    str(receipt_id),
                ),
            )
            conn.commit()
            updated = conn.execute(
                _subject_deletion_sql("SELECT * FROM {subject_deletion_table} WHERE receipt_id=?"),
                (str(receipt_id),),
            ).fetchone()
            assert updated is not None
            return self._subject_deletion_receipt(updated)

    def bind_subject_deletion_mutation(
        self,
        receipt_id: str,
        mutation_id: str,
    ) -> dict[str, Any]:
        """Bind a prepared receipt to its append-only Wiki tombstone mutation."""

        with self._conn() as conn:
            receipt = conn.execute(
                _subject_deletion_sql("SELECT * FROM {subject_deletion_table} WHERE receipt_id=?"),
                (str(receipt_id),),
            ).fetchone()
            if receipt is None:
                raise KeyError(f"unknown Wiki subject deletion receipt: {receipt_id}")
            mutation = conn.execute(
                "SELECT * FROM wiki_mutations WHERE mutation_id=?",
                (str(mutation_id),),
            ).fetchone()
            if mutation is None:
                raise LookupError(f"unknown Wiki deletion mutation: {mutation_id}")
            if (
                str(mutation["page_id"]) != str(receipt["page_id"])
                or str(mutation["mutation_type"]) != "delete"
                or not bool(mutation["tombstone"])
            ):
                raise ValueError("Wiki deletion mutation does not bind the receipt page tombstone")
            existing = str(receipt["mutation_id"] or "")
            if existing and existing != str(mutation_id):
                raise ValueError("Wiki subject deletion mutation is immutable")
            if str(receipt["status"]) == "applied":
                return self._subject_deletion_receipt(receipt)
            if str(receipt["status"]) not in {"planned", "tombstoned"}:
                raise RuntimeError("Wiki subject deletion must be approved before tombstoning")
            conn.execute(
                _subject_deletion_sql("""
                UPDATE {subject_deletion_table}
                SET mutation_id=?, status='tombstoned', tombstoned_at=?
                WHERE receipt_id=?
                """),
                (str(mutation_id), _now(), str(receipt_id)),
            )
            conn.commit()
            updated = conn.execute(
                _subject_deletion_sql("SELECT * FROM {subject_deletion_table} WHERE receipt_id=?"),
                (str(receipt_id),),
            ).fetchone()
            assert updated is not None
            return self._subject_deletion_receipt(updated)

    def mark_subject_deletion_applied(
        self,
        receipt_id: str,
        *,
        event_trace_id: str,
    ) -> dict[str, Any]:
        """Mark the file deletion applied only after its delete event is durable."""

        with self._conn() as conn:
            receipt = conn.execute(
                _subject_deletion_sql("SELECT * FROM {subject_deletion_table} WHERE receipt_id=?"),
                (str(receipt_id),),
            ).fetchone()
            if receipt is None:
                raise KeyError(f"unknown Wiki subject deletion receipt: {receipt_id}")
            if str(receipt["status"]) not in {"tombstoned", "applied"}:
                raise RuntimeError("Wiki subject deletion is not tombstoned")
            mutation_id = str(receipt["mutation_id"] or "")
            if not mutation_id:
                raise RuntimeError("Wiki subject deletion mutation is missing")
            mutation = conn.execute(
                "SELECT event_trace_id FROM wiki_mutations WHERE mutation_id=?",
                (mutation_id,),
            ).fetchone()
            if mutation is None:
                raise LookupError(f"Wiki subject deletion mutation disappeared: {mutation_id}")
            durable_trace = str(mutation["event_trace_id"] or "")
            if not durable_trace or durable_trace != str(event_trace_id):
                raise ValueError("Wiki subject deletion event trace is not durably bound")
            existing = str(receipt["event_trace_id"] or "")
            if existing and existing != durable_trace:
                raise ValueError("Wiki subject deletion event trace is immutable")
            if str(receipt["status"]) != "applied":
                conn.execute(
                    _subject_deletion_sql("""
                    UPDATE {subject_deletion_table}
                    SET status='applied', event_trace_id=?, applied_at=?
                    WHERE receipt_id=?
                    """),
                    (durable_trace, _now(), str(receipt_id)),
                )
                conn.commit()
            updated = conn.execute(
                _subject_deletion_sql("SELECT * FROM {subject_deletion_table} WHERE receipt_id=?"),
                (str(receipt_id),),
            ).fetchone()
            assert updated is not None
            return self._subject_deletion_receipt(updated)

    def subject_deletion_receipts_for_scope(
        self,
        *,
        scope_kind: str,
        scope_value_hash: str,
    ) -> list[dict[str, Any]]:
        """Return opaque receipts for one subject scope in creation order."""

        with self._conn() as conn:
            rows = conn.execute(
                _subject_deletion_sql("""
                SELECT * FROM {subject_deletion_table}
                WHERE scope_kind=? AND scope_value_hash=?
                ORDER BY created_at, receipt_id
                """),
                (str(scope_kind), str(scope_value_hash)),
            ).fetchall()
        return [self._subject_deletion_receipt(row) for row in rows]

    def required_consumer_gaps(
        self,
        mutation_id: str,
        required_consumers: Sequence[str] = DEFAULT_REQUIRED_CONSUMERS,
    ) -> list[str]:
        """Return consumers that have not terminally applied one mutation."""

        consumers = tuple(dict.fromkeys(str(value) for value in required_consumers if value))
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT consumer, outcome FROM projection_receipts
                WHERE mutation_id=?
                """,
                (str(mutation_id),),
            ).fetchall()
        outcomes = {str(row["consumer"]): str(row["outcome"]) for row in rows}
        return [
            consumer
            for consumer in consumers
            if outcomes.get(consumer) not in TERMINAL_RECEIPT_OUTCOMES
        ]

    @staticmethod
    def _revision(
        page_id: str,
        parent_revision: str,
        mutation_type: str,
        page_path: str,
        previous_path: str,
        content_sha256: str,
    ) -> str:
        payload = json.dumps(
            [
                page_id,
                parent_revision,
                mutation_type,
                page_path,
                previous_path,
                content_sha256,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def record_mutation(
        self,
        page_path: Path | str,
        *,
        mutation_type: str,
        previous_path: Path | str | None = None,
        page_id: str = "",
        force: bool = False,
        expected_content_sha256: str = "",
    ) -> WikiMutationReceipt:
        """Append one mutation while preserving page identity and causal revision order.

        ``expected_content_sha256`` is the write-ahead contract used by typed
        projection publishers.  It lets the ledger bind the intended bytes
        before the filesystem effect; ordinary callers continue to derive the
        digest from the already-written file.
        """

        mutation_type = str(mutation_type).strip().lower()
        if mutation_type not in MUTATION_TYPES:
            raise ValueError(f"unsupported wiki mutation_type: {mutation_type}")

        current_path = _normalized_path(page_path)
        old_path = _normalized_path(previous_path) if previous_path else ""
        file_path = Path(current_path)
        declared_hash = (
            _declared_content_sha256(expected_content_sha256)
            if expected_content_sha256
            else ""
        )
        if mutation_type == "delete" and declared_hash:
            raise ValueError("delete mutations derive their hash from the current page identity")
        if mutation_type != "delete" and not declared_hash and not file_path.is_file():
            raise FileNotFoundError(current_path)

        now = _now()
        with self._conn() as conn:
            current = conn.execute(
                "SELECT * FROM wiki_pages WHERE current_path=?",
                (current_path,),
            ).fetchone()
            previous = (
                conn.execute(
                    "SELECT * FROM wiki_pages WHERE current_path=?", (old_path,)
                ).fetchone()
                if old_path
                else None
            )
            identified = current or previous
            if page_id:
                identified = (
                    conn.execute("SELECT * FROM wiki_pages WHERE page_id=?", (page_id,)).fetchone()
                    or identified
                )
            if mutation_type in {"move", "delete"} and identified is None:
                raise LookupError(
                    f"wiki {mutation_type} requires an existing page identity: {old_path or current_path}"
                )

            stable_page_id = (
                str(identified["page_id"]) if identified else (page_id or uuid.uuid4().hex)
            )
            if mutation_type == "delete":
                assert identified is not None
                content_hash = str(identified["content_sha256"])
                target_path = str(identified["current_path"])
                old_path = old_path or target_path
                state = "tombstone"
            else:
                content_hash = declared_hash or _content_sha256(file_path)
                target_path = current_path
                state = "active"
                if mutation_type == "move" and not old_path:
                    assert identified is not None
                    old_path = str(identified["current_path"])

            parent_revision = str(identified["current_revision"]) if identified else ""
            if not force and identified and (
                str(identified["current_path"]) == target_path
                and str(identified["content_sha256"]) == content_hash
                and str(identified["lifecycle_state"]) == state
            ):
                no_op = conn.execute(
                    "SELECT * FROM wiki_mutations WHERE page_id=? AND page_revision=?",
                    (stable_page_id, parent_revision),
                ).fetchone()
                if no_op is not None:
                    return self._receipt(no_op)

            revision = self._revision(
                stable_page_id,
                parent_revision,
                mutation_type,
                target_path,
                old_path,
                content_hash,
            )
            existing = conn.execute(
                "SELECT * FROM wiki_mutations WHERE page_id=? AND page_revision=?",
                (stable_page_id, revision),
            ).fetchone()
            if existing:
                raise RuntimeError(
                    "refusing to move a Wiki page pointer backward to a historical revision"
                )

            if identified:
                conn.execute(
                    """UPDATE wiki_pages
                       SET current_path=?, current_revision=?, content_sha256=?,
                           lifecycle_state=?, updated_at=?
                       WHERE page_id=?""",
                    (target_path, revision, content_hash, state, now, stable_page_id),
                )
            else:
                conn.execute(
                    """INSERT INTO wiki_pages
                       (page_id, current_path, current_revision, content_sha256,
                        lifecycle_state, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (stable_page_id, target_path, revision, content_hash, state, now),
                )

            mutation_id = (
                "wiki-mut-"
                + hashlib.sha256(f"{stable_page_id}:{revision}".encode("utf-8")).hexdigest()[:24]
            )
            sequence_no = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM wiki_mutations"
                ).fetchone()[0]
            )
            conn.execute(
                """INSERT INTO wiki_mutations
                   (mutation_id, page_id, page_revision, parent_revision, sequence_no,
                    mutation_type, page_path,
                    previous_path, content_sha256, tombstone, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mutation_id,
                    stable_page_id,
                    revision,
                    parent_revision,
                    sequence_no,
                    mutation_type,
                    target_path,
                    old_path,
                    content_hash,
                    int(state == "tombstone"),
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM wiki_mutations WHERE mutation_id=?", (mutation_id,)
            ).fetchone()
            assert row is not None
            return self._receipt(row)

    def attach_event(self, mutation_id: str, trace_id: str) -> None:
        """Attach the first stable producer trace without rewriting provenance."""

        if not trace_id:
            raise ValueError("trace_id must not be empty")

        with self._conn() as conn:
            row = conn.execute(
                "SELECT event_trace_id FROM wiki_mutations WHERE mutation_id=?",
                (mutation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown Wiki mutation: {mutation_id}")
            existing = str(row["event_trace_id"] or "")
            if existing and existing != trace_id:
                raise ValueError(f"producer trace is immutable for {mutation_id}: {existing}")
            if not existing:
                conn.execute(
                    "UPDATE wiki_mutations SET event_trace_id=? WHERE mutation_id=?",
                    (trace_id, mutation_id),
                )
            conn.commit()

    def record_projection_receipt(
        self,
        *,
        mutation_id: str,
        consumer: str,
        outcome: str,
        reason: str = "",
        event_trace_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist a consumer outcome without allowing causal revision skips."""

        outcome = str(outcome).strip().lower()
        if outcome not in {"ack", "noop", "retry", "dead"}:
            raise ValueError(f"unsupported projection outcome: {outcome}")
        with self._conn() as conn:
            effective_event_trace_id = event_trace_id
            mutation = conn.execute(
                "SELECT page_id, page_revision FROM wiki_mutations WHERE mutation_id=?",
                (mutation_id,),
            ).fetchone()
            if mutation is None:
                raise LookupError(f"unknown wiki mutation: {mutation_id}")
            existing_receipt = conn.execute(
                """SELECT outcome, event_trace_id FROM projection_receipts
                   WHERE mutation_id=? AND consumer=?""",
                (mutation_id, consumer),
            ).fetchone()
            if existing_receipt is not None:
                existing_trace = str(existing_receipt["event_trace_id"] or "")
                if existing_trace and event_trace_id and existing_trace != event_trace_id:
                    raise ValueError(
                        "projection receipt trace is immutable: "
                        f"{existing_trace} != {event_trace_id}"
                    )
                if existing_trace:
                    effective_event_trace_id = existing_trace
                if str(existing_receipt["outcome"]) in TERMINAL_RECEIPT_OUTCOMES:
                    return
            if outcome in TERMINAL_RECEIPT_OUTCOMES:
                predecessor = conn.execute(
                    """SELECT previous.mutation_id
                       FROM wiki_mutations AS current
                       JOIN wiki_mutations AS previous
                         ON previous.page_id=current.page_id
                        AND previous.sequence_no < current.sequence_no
                       LEFT JOIN projection_receipts AS receipt
                         ON receipt.mutation_id=previous.mutation_id
                        AND receipt.consumer=?
                       WHERE current.mutation_id=?
                         AND (receipt.mutation_id IS NULL OR receipt.outcome NOT IN ('ack', 'noop'))
                       ORDER BY previous.sequence_no LIMIT 1""",
                    (consumer, mutation_id),
                ).fetchone()
                if predecessor is not None:
                    raise RuntimeError(
                        "cannot acknowledge Wiki projection before predecessor revision: "
                        f"{predecessor['mutation_id']}"
                    )
            conn.execute(
                """INSERT INTO projection_receipts
                   (mutation_id, page_id, page_revision, consumer, outcome, reason,
                    event_trace_id, metadata_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mutation_id, consumer) DO UPDATE SET
                       outcome=excluded.outcome,
                       reason=excluded.reason,
                       event_trace_id=excluded.event_trace_id,
                       metadata_json=excluded.metadata_json,
                       updated_at=excluded.updated_at""",
                (
                    mutation_id,
                    mutation["page_id"],
                    mutation["page_revision"],
                    consumer,
                    outcome,
                    reason,
                    effective_event_trace_id,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    _now(),
                ),
            )
            conn.commit()

    def first_unacknowledged_predecessor(self, mutation_id: str, consumer: str) -> str | None:
        """Return the oldest causal predecessor not completed by this consumer."""

        with self._conn() as conn:
            row = conn.execute(
                """SELECT previous.mutation_id
                   FROM wiki_mutations AS current
                   JOIN wiki_mutations AS previous
                     ON previous.page_id=current.page_id
                    AND previous.sequence_no < current.sequence_no
                   LEFT JOIN projection_receipts AS receipt
                     ON receipt.mutation_id=previous.mutation_id
                    AND receipt.consumer=?
                   WHERE current.mutation_id=?
                     AND (receipt.mutation_id IS NULL OR receipt.outcome NOT IN ('ack', 'noop'))
                   ORDER BY previous.sequence_no LIMIT 1""",
                (consumer, mutation_id),
            ).fetchone()
        return str(row["mutation_id"]) if row is not None else None

    def terminal_projection_receipt(self, mutation_id: str, consumer: str) -> dict[str, str] | None:
        """Return an existing completed consumer watermark, if any."""

        with self._conn() as conn:
            row = conn.execute(
                """SELECT outcome, reason, event_trace_id
                   FROM projection_receipts
                   WHERE mutation_id=? AND consumer=? AND outcome IN ('ack', 'noop')""",
                (mutation_id, consumer),
            ).fetchone()
        return dict(row) if row is not None else None

    def projection_receipt(self, mutation_id: str, consumer: str) -> dict[str, str] | None:
        """Return the current consumer attempt without changing its causal trace."""

        with self._conn() as conn:
            row = conn.execute(
                """SELECT outcome, reason, event_trace_id
                   FROM projection_receipts
                   WHERE mutation_id=? AND consumer=?""",
                (mutation_id, consumer),
            ).fetchone()
        return dict(row) if row is not None else None

    def material_projection_effect(self, effect_id: str) -> dict[str, Any] | None:
        """Read one exact material consumer attempt without mutating it."""

        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM wiki_projection_material_effects WHERE effect_id=?",
                (str(effect_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def begin_material_projection_effect(
        self,
        *,
        effect_id: str,
        command_id: str,
        source_id: str,
        consumer: str,
        target_ref: str,
        input_hash: str,
        before_hash: str,
        started_at: str,
    ) -> None:
        """Persist an at-most-once intent before invoking a consumer."""

        identity = (
            str(command_id),
            str(source_id),
            str(consumer),
            str(target_ref),
            str(input_hash),
        )
        empty_outcome: dict[str, Any] = {}
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM wiki_projection_material_effects WHERE effect_id=?",
                (str(effect_id),),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO wiki_projection_material_effects(
                        effect_id, command_id, source_id, consumer, target_ref,
                        input_hash, before_hash, after_hash, status, reason_code,
                        outcome_json, outcome_hash, attempt_count, started_at,
                        completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'executing', '', ?, ?, 1, ?, '')
                    """,
                    (
                        str(effect_id),
                        *identity,
                        str(before_hash),
                        str(before_hash),
                        json.dumps(empty_outcome, sort_keys=True, separators=(",", ":")),
                        sha256_json(empty_outcome),
                        str(started_at),
                    ),
                )
                conn.commit()
                return
            stored_identity = tuple(
                str(row[key])
                for key in (
                    "command_id",
                    "source_id",
                    "consumer",
                    "target_ref",
                    "input_hash",
                )
            )
            if stored_identity != identity:
                raise RuntimeError("wiki projection material effect identity is immutable")
            if str(row["status"]) != "retryable":
                raise RuntimeError("wiki projection material effect is already active or terminal")
            if str(row["after_hash"]) != str(before_hash):
                raise RuntimeError("retryable Wiki projection target drifted before retry")
            conn.execute(
                """
                UPDATE wiki_projection_material_effects
                SET before_hash=?, after_hash=?, status='executing',
                    reason_code='', outcome_json=?, outcome_hash=?,
                    attempt_count=attempt_count+1, started_at=?, completed_at=''
                WHERE effect_id=? AND status='retryable'
                """,
                (
                    str(before_hash),
                    str(before_hash),
                    json.dumps(empty_outcome, sort_keys=True, separators=(",", ":")),
                    sha256_json(empty_outcome),
                    str(started_at),
                    str(effect_id),
                ),
            )
            conn.commit()

    def finalize_material_projection_effect(
        self,
        *,
        effect_id: str,
        status: str,
        after_hash: str,
        reason_code: str,
        outcome: dict[str, Any],
        completed_at: str,
    ) -> None:
        """Durably classify an attempted consumer result before canonical close."""

        normalized_status = str(status)
        if normalized_status not in {"retryable", "committed", "dead_letter"}:
            raise ValueError("unsupported Wiki projection material effect status")
        outcome_payload = dict(outcome)
        outcome_json = json.dumps(
            outcome_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT before_hash, status
                FROM wiki_projection_material_effects WHERE effect_id=?
                """,
                (str(effect_id),),
            ).fetchone()
            if row is None or str(row["status"]) != "executing":
                raise RuntimeError(
                    "Wiki projection material effect is missing or already finalized"
                )
            if normalized_status in {"retryable", "dead_letter"} and str(row["before_hash"]) != str(
                after_hash
            ):
                raise RuntimeError(
                    "non-success Wiki projection changed target state without rollback"
                )
            conn.execute(
                """
                UPDATE wiki_projection_material_effects
                SET after_hash=?, status=?, reason_code=?, outcome_json=?,
                    outcome_hash=?, completed_at=?
                WHERE effect_id=? AND status='executing'
                """,
                (
                    str(after_hash),
                    normalized_status,
                    str(reason_code),
                    outcome_json,
                    sha256_json(outcome_payload),
                    str(completed_at),
                    str(effect_id),
                ),
            )
            conn.commit()

    def reconciliation_report(
        self,
        required_consumers: Sequence[str] = DEFAULT_REQUIRED_CONSUMERS,
    ) -> dict[str, Any]:
        """Report missing or non-terminal receipts for every required consumer."""

        consumers = tuple(dict.fromkeys(str(item) for item in required_consumers if item))
        with self._conn() as conn:
            mutations = conn.execute(
                "SELECT mutation_id, page_id, page_revision, mutation_type, page_path FROM wiki_mutations"
            ).fetchall()
            receipts = conn.execute(
                "SELECT mutation_id, consumer, outcome, reason FROM projection_receipts"
            ).fetchall()
            pointer_rows = conn.execute(
                """SELECT page.page_id, page.current_path, page.current_revision,
                          page.lifecycle_state, latest.page_path, latest.page_revision,
                          latest.tombstone
                   FROM wiki_pages AS page
                   JOIN wiki_mutations AS latest ON latest.mutation_id=(
                     SELECT mutation_id FROM wiki_mutations
                     WHERE page_id=page.page_id ORDER BY sequence_no DESC LIMIT 1
                   )
                   WHERE page.current_path<>latest.page_path
                      OR page.current_revision<>latest.page_revision
                      OR page.lifecycle_state<>(
                        CASE WHEN latest.tombstone=1 THEN 'tombstone' ELSE 'active' END
                      )"""
            ).fetchall()
            pending_subject_deletions = conn.execute(_subject_deletion_sql("""
                SELECT receipt_id, mutation_id, status
                FROM {subject_deletion_table}
                WHERE status <> 'applied'
                ORDER BY created_at, receipt_id
                """)).fetchall()
        by_mutation: dict[str, dict[str, sqlite3.Row]] = {}
        for receipt in receipts:
            by_mutation.setdefault(receipt["mutation_id"], {})[receipt["consumer"]] = receipt
        gaps: list[dict[str, Any]] = []
        for mutation in mutations:
            actual = by_mutation.get(mutation["mutation_id"], {})
            missing = [name for name in consumers if name not in actual]
            failed = [
                name
                for name in consumers
                if name in actual and actual[name]["outcome"] not in TERMINAL_RECEIPT_OUTCOMES
            ]
            if missing or failed:
                gaps.append(
                    {
                        "mutation_id": mutation["mutation_id"],
                        "page_id": mutation["page_id"],
                        "page_revision": mutation["page_revision"],
                        "mutation_type": mutation["mutation_type"],
                        "page_path": mutation["page_path"],
                        "missing_consumers": missing,
                        "failed_consumers": failed,
                    }
                )
        pointer_gaps = [dict(row) for row in pointer_rows]
        subject_deletion_gaps = [dict(row) for row in pending_subject_deletions]
        return {
            "schema_version": "mnemos.wiki_projection_reconciliation.v1",
            "required_consumers": list(consumers),
            "mutation_count": len(mutations),
            "receipt_count": len(receipts),
            "receipt_gap": len(gaps),
            "pointer_gap": len(pointer_gaps),
            "subject_deletion_gap": len(subject_deletion_gaps),
            "projection_gap": len(gaps) + len(pointer_gaps) + len(subject_deletion_gaps),
            "gaps": gaps,
            "pointer_gaps": pointer_gaps,
            "subject_deletion_gaps": subject_deletion_gaps,
            "ok": not gaps and not pointer_gaps and not subject_deletion_gaps,
        }

    def unpublished_mutations(self, limit: int = 100) -> list[WikiMutationReceipt]:
        """Return unreconciled mutations not yet linked to an EventBus trace."""

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT mutation.* FROM wiki_mutations AS mutation
                    WHERE mutation.event_trace_id=''
                      AND (
                        SELECT COUNT(DISTINCT receipt.consumer)
                        FROM projection_receipts AS receipt
                        WHERE receipt.mutation_id=mutation.mutation_id
                          AND receipt.consumer IN (?, ?, ?, ?, ?, ?)
                          AND receipt.outcome IN ('ack', 'noop')
                      ) < ?
                    ORDER BY mutation.sequence_no LIMIT ?""",
                (
                    *DEFAULT_REQUIRED_CONSUMERS,
                    len(DEFAULT_REQUIRED_CONSUMERS),
                    max(1, int(limit)),
                ),
            ).fetchall()
        return [self._receipt(row) for row in rows]

    def repair_synthetic_rebuild_event_traces(self) -> int:
        """Clear provenance values written by the former rebuild overwrite bug."""

        with self._conn() as conn:
            cursor = conn.execute("""UPDATE wiki_mutations
                   SET event_trace_id=''
                   WHERE event_trace_id LIKE 'wiki-rebuild-%'""")
            conn.commit()
            return max(0, int(cursor.rowcount))

    def repair_current_pointers_from_history(self) -> int:
        """Restore materialized page pointers from the latest causal mutation."""

        repaired = 0
        with self._conn() as conn:
            latest = conn.execute("""SELECT mutation.*
                   FROM wiki_mutations AS mutation
                   JOIN (
                     SELECT page_id, MAX(sequence_no) AS sequence_no
                     FROM wiki_mutations GROUP BY page_id
                   ) AS newest
                     ON newest.page_id=mutation.page_id
                    AND newest.sequence_no=mutation.sequence_no""").fetchall()
            for mutation in latest:
                page = conn.execute(
                    "SELECT * FROM wiki_pages WHERE page_id=?",
                    (mutation["page_id"],),
                ).fetchone()
                expected_state = "tombstone" if mutation["tombstone"] else "active"
                if page is not None and (
                    str(page["current_path"]) == str(mutation["page_path"])
                    and str(page["current_revision"]) == str(mutation["page_revision"])
                    and str(page["content_sha256"]) == str(mutation["content_sha256"])
                    and str(page["lifecycle_state"]) == expected_state
                ):
                    continue
                if page is None:
                    raise RuntimeError(
                        f"mutation history has no materialized page: {mutation['page_id']}"
                    )
                conn.execute(
                    """UPDATE wiki_pages
                       SET current_path=?, current_revision=?, content_sha256=?,
                           lifecycle_state=?, updated_at=?
                       WHERE page_id=?""",
                    (
                        mutation["page_path"],
                        mutation["page_revision"],
                        mutation["content_sha256"],
                        expected_state,
                        _now(),
                        mutation["page_id"],
                    ),
                )
                repaired += 1
            conn.commit()
        return repaired

    def reconcile_vault(self, vault_dir: Path | str) -> dict[str, Any]:
        """Record direct/manual create, update, move, and delete mutations.

        Exact content hash is used only to pair a disappeared path with one new
        path during the same scan. Ambiguous pairs remain delete+create instead
        of inventing identity.
        """

        root = Path(vault_dir).expanduser().resolve(strict=True)
        files = {
            str(path.resolve()): _content_sha256(path)
            for path in root.rglob("*.md")
            if _is_vault_content_path(path, root)
        }
        with self._conn() as conn:
            active_rows = conn.execute(
                "SELECT * FROM wiki_pages WHERE lifecycle_state='active'"
            ).fetchall()
        active = {
            str(row["current_path"]): row
            for row in active_rows
            if _is_vault_content_path(row["current_path"], root)
        }
        missing = {path: row for path, row in active.items() if path not in files}
        new_paths = {path: digest for path, digest in files.items() if path not in active}

        missing_by_hash: dict[str, list[sqlite3.Row]] = {}
        for row in missing.values():
            missing_by_hash.setdefault(str(row["content_sha256"]), []).append(row)
        new_by_hash: dict[str, list[str]] = {}
        for path, digest in new_paths.items():
            new_by_hash.setdefault(digest, []).append(path)

        moved_old: set[str] = set()
        moved_new: set[str] = set()
        receipts: list[WikiMutationReceipt] = []
        for digest, old_rows in missing_by_hash.items():
            candidates = new_by_hash.get(digest, [])
            if len(old_rows) == 1 and len(candidates) == 1:
                old_row = old_rows[0]
                new_path = candidates[0]
                receipts.append(
                    self.record_mutation(
                        new_path,
                        mutation_type="move",
                        previous_path=old_row["current_path"],
                        page_id=old_row["page_id"],
                    )
                )
                moved_old.add(str(old_row["current_path"]))
                moved_new.add(new_path)

        counts = {"create": 0, "update": 0, "move": len(moved_new), "delete": 0}
        for path, digest in files.items():
            row = active.get(path)
            if row and str(row["content_sha256"]) != digest:
                receipts.append(self.record_mutation(path, mutation_type="update"))
                counts["update"] += 1
            elif path not in active and path not in moved_new:
                receipts.append(self.record_mutation(path, mutation_type="create"))
                counts["create"] += 1
        for path in missing:
            if path not in moved_old:
                receipts.append(self.record_mutation(path, mutation_type="delete"))
                counts["delete"] += 1

        return {
            "schema_version": "mnemos.wiki_mutation_scan.v1",
            "vault_dir": str(root),
            "scanned_pages": len(files),
            "recorded_mutations": len(receipts),
            "counts": counts,
            "mutations": [receipt.to_dict() for receipt in receipts],
        }

    def prune_out_of_scope_pages(self, vault_dir: Path | str) -> dict[str, int]:
        """Remove scanner-created ledger rows that are not Wiki content.

        This is a narrow repair for old scanners that admitted hidden projection
        artifacts (for example ``.kg/snapshots``). User-visible page history stays
        append-only; only paths outside the ledger's documented Vault scope are
        eligible for deletion.
        """

        root = Path(vault_dir).expanduser().resolve(strict=True)
        with self._conn() as conn:
            rows = conn.execute("SELECT page_id, current_path FROM wiki_pages").fetchall()
            page_ids = [
                str(row["page_id"])
                for row in rows
                if not _is_vault_content_path(row["current_path"], root)
            ]
            if not page_ids:
                return {"pages": 0, "mutations": 0, "projection_receipts": 0}

            mutation_rows = [
                row
                for page_id in page_ids
                for row in conn.execute(
                    "SELECT mutation_id FROM wiki_mutations WHERE page_id=?", (page_id,)
                ).fetchall()
            ]
            mutation_ids = [str(row["mutation_id"]) for row in mutation_rows]
            receipt_count = sum(
                int(
                    conn.execute(
                        "SELECT COUNT(*) FROM projection_receipts WHERE mutation_id=?",
                        (mutation_id,),
                    ).fetchone()[0]
                )
                for mutation_id in mutation_ids
            )
            conn.executemany(
                "DELETE FROM projection_receipts WHERE mutation_id=?",
                ((mutation_id,) for mutation_id in mutation_ids),
            )
            conn.executemany(
                "DELETE FROM wiki_mutations WHERE page_id=?",
                ((page_id,) for page_id in page_ids),
            )
            conn.executemany(
                "DELETE FROM wiki_pages WHERE page_id=?",
                ((page_id,) for page_id in page_ids),
            )
            conn.commit()
        return {
            "pages": len(page_ids),
            "mutations": len(mutation_ids),
            "projection_receipts": receipt_count,
        }

    def list_mutations(self) -> list[dict[str, Any]]:
        """Return the complete append-only mutation history in causal order."""

        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM wiki_mutations ORDER BY sequence_no").fetchall()
        return [self._receipt(row).to_dict() for row in rows]

    def get_mutation(self, mutation_id: str) -> dict[str, Any] | None:
        """Return one authoritative mutation by stable id."""

        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM wiki_mutations WHERE mutation_id=?", (mutation_id,)
            ).fetchone()
        return self._receipt(row).to_dict() if row is not None else None

    def mutation_receipt(self, mutation_id: str) -> WikiMutationReceipt | None:
        """Return the typed mutation receipt needed for a targeted republish."""

        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM wiki_mutations WHERE mutation_id=?",
                (str(mutation_id),),
            ).fetchone()
        return self._receipt(row) if row is not None else None


def projection_snapshot_hash(rows: Iterable[dict[str, Any]]) -> str:
    """Stable comparator used for incremental versus full rebuild evidence."""

    normalized = [dict(sorted(row.items())) for row in rows]
    normalized.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
