"""Typed, replayable lifecycle for canonical-store-derived Wiki pages.

The filesystem is only a projection target.  Every generated page is bound to
an immutable canonical revision in a generation manifest, written atomically,
recorded by :class:`WikiProjectionLedger`, and then published through the
canonical Wiki mutation publisher.  A failed write or event therefore remains
visible and can be replayed without re-running the upstream extractor.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from core.file_ops import atomic_write_text, sha256_file

if TYPE_CHECKING:
    from core.wiki_projection_lifecycle import WikiMutationReceipt, WikiProjectionLedger


DERIVED_PROJECTION_SCHEMA_VERSION = "mnemos.derived_projection.v1"
# Exporters with their own byte budgets reserve this small, deterministic
# envelope for the lifecycle frontmatter injected at publication time.
PROJECTION_BINDING_RESERVE_BYTES = 512
_PAGE_ROLE_PREFIXES = ("formal_derived:", "derived_report:")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    return "sha256:" + sha256_file(path)


def _json_value(value: Any) -> Any:
    """Return a deterministic JSON value for a canonical source object."""

    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value.expanduser().resolve(strict=False))
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, frozenset)):
        normalized = [_json_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonical_projection_revision(value: Any) -> str:
    """Hash the complete canonical input that determines one projection."""

    payload = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(payload)


@dataclass(frozen=True)
class ProjectionPageSpec:
    """One desired Markdown page and its immutable canonical binding."""

    path: Path
    content: str
    page_role: str
    canonical_revision: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class DerivedProjectionMutationReceipt:
    """Terminal or replayable receipt for one generation item."""

    generation_id: str
    projection_kind: str
    path: str
    page_role: str
    canonical_revision: str
    content_sha256: str
    action: str
    status: str
    mutation_id: str
    page_revision: str
    event_trace_id: str
    error: str


@dataclass(frozen=True)
class ProjectionGenerationReceipt:
    """Durable manifest receipt for a full or incremental generation."""

    generation_id: str
    projection_kind: str
    scope_root: str
    manifest_hash: str
    full_generation: bool
    status: str
    expected_item_count: int
    published_count: int
    items: tuple[DerivedProjectionMutationReceipt, ...]


@dataclass(frozen=True)
class DerivedProjectionMutationAuthorization:
    """Exact write/delete authority bound to a durable mutation receipt."""

    generation_id: str
    mutation_id: str
    page_revision: str
    target_path: str
    content_sha256: str
    action: str

    def _assert_target(self, path: Path) -> None:
        target = str(path.expanduser().resolve(strict=False))
        if target != self.target_path:
            raise ValueError("derived projection authorization target mismatch")

    def assert_upsert(self, path: Path, content: str) -> None:
        """Validate the exact path, action, and bytes authorized for upsert."""

        self._assert_target(path)
        if self.action != "upsert":
            raise ValueError("derived projection authorization is not an upsert")
        if _sha256_text(content) != self.content_sha256:
            raise ValueError("derived projection authorization content hash mismatch")

    def assert_delete(self, path: Path) -> None:
        """Validate the exact path and preimage authorized for deletion."""

        self._assert_target(path)
        if self.action != "delete":
            raise ValueError("derived projection authorization is not a delete")
        if path.is_file() and _sha256_file(path) != self.content_sha256:
            raise ValueError("derived projection delete content hash mismatch")


@dataclass(frozen=True)
class _PreparedItem:
    path: Path
    content: str
    page_role: str
    canonical_revision: str
    source_refs: tuple[str, ...]
    content_sha256: str
    action: str


class DerivedProjectionLifecycle:
    """Coordinate generation manifests with Wiki mutations and publishing."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS derived_projection_generations (
        generation_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        projection_kind TEXT NOT NULL,
        scope_root TEXT NOT NULL,
        manifest_hash TEXT NOT NULL,
        full_generation INTEGER NOT NULL,
        expected_item_count INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running', 'committed', 'failed')),
        error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_derived_projection_generation_scope
        ON derived_projection_generations(projection_kind, scope_root, updated_at);

    CREATE TABLE IF NOT EXISTS derived_projection_generation_items (
        generation_id TEXT NOT NULL,
        target_path TEXT NOT NULL,
        page_role TEXT NOT NULL,
        canonical_revision TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        source_refs_json TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('upsert', 'delete')),
        status TEXT NOT NULL CHECK(
            status IN ('planned', 'mutation_recorded', 'published', 'failed')
        ),
        mutation_id TEXT NOT NULL DEFAULT '',
        page_revision TEXT NOT NULL DEFAULT '',
        event_trace_id TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY(generation_id, target_path),
        FOREIGN KEY(generation_id)
            REFERENCES derived_projection_generations(generation_id)
    );
    CREATE INDEX IF NOT EXISTS idx_derived_projection_item_target
        ON derived_projection_generation_items(target_path, updated_at);
    CREATE INDEX IF NOT EXISTS idx_derived_projection_item_mutation
        ON derived_projection_generation_items(mutation_id);
    """

    def __init__(
        self,
        vault_dir: Path | str,
        *,
        ledger: WikiProjectionLedger | None = None,
        event_bus: Any | None = None,
        file_writer: Callable[
            [DerivedProjectionMutationAuthorization, Path, str], None
        ]
        | None = None,
    ):
        self.vault_dir = Path(vault_dir).expanduser().resolve(strict=False)
        if ledger is None:
            from core.wiki_projection_lifecycle import WikiProjectionLedger

            ledger = WikiProjectionLedger()
        self.ledger = ledger
        self.event_bus = event_bus
        self._file_writer = file_writer or self._atomic_publish
        self._initialize_manifest_schema()

    @staticmethod
    def _atomic_publish(
        authorization: DerivedProjectionMutationAuthorization,
        path: Path,
        content: str,
    ) -> None:
        """Commit exact rendered bytes through the shared atomic primitive."""

        authorization.assert_upsert(path, content)
        atomic_write_text(path, content, encoding="utf-8")

    @staticmethod
    def _atomic_delete(
        authorization: DerivedProjectionMutationAuthorization,
        path: Path,
    ) -> None:
        """Delete only the exact bytes bound by the durable mutation receipt."""

        authorization.assert_delete(path)
        path.unlink()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.ledger.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize_manifest_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(self._SCHEMA)
            conn.commit()

    def publish_generation(
        self,
        *,
        projection_kind: str,
        scope_root: Path | str,
        pages: Sequence[ProjectionPageSpec],
        full: bool,
        stale_paths: Iterable[Path | str] = (),
        owned_paths: Iterable[Path | str] | None = None,
    ) -> ProjectionGenerationReceipt:
        """Publish one deterministic generation or resume its failed attempt."""

        self._validate_publisher_binding()
        kind = str(projection_kind or "").strip()
        if not kind:
            raise ValueError("projection_kind must not be empty")
        scope = self._validated_scope(scope_root)
        prepared = self._prepare_pages(kind, scope, pages)
        desired_paths = {item.path for item in prepared}
        stale = {
            self._validated_target(Path(path), scope)
            for path in stale_paths
        }
        managed = (
            {
                self._validated_target(Path(path), scope)
                for path in owned_paths
            }
            if owned_paths is not None
            else None
        )
        if managed is not None and not full:
            raise ValueError("owned_paths is only valid for a full generation")
        if managed is not None and not desired_paths <= managed:
            raise ValueError("derived projection page is outside its managed path set")
        if full:
            if managed is None:
                stale.update(self._full_generation_stale_paths(kind, scope, desired_paths))
            else:
                stale.update(managed)
                stale.update(self._active_manifest_paths(kind, scope))
        stale.difference_update(desired_paths)
        prepared.extend(self._prepare_deletions(stale))
        prepared.sort(key=lambda item: (str(item.path), item.action))

        manifest_hash = canonical_projection_revision(
            {
                "schema_version": DERIVED_PROJECTION_SCHEMA_VERSION,
                "projection_kind": kind,
                "scope_root": str(scope),
                "full_generation": bool(full),
                "owned_paths": (
                    sorted(str(path) for path in managed)
                    if managed is not None
                    else None
                ),
                "items": [
                    {
                        "path": str(item.path),
                        "page_role": item.page_role,
                        "canonical_revision": item.canonical_revision,
                        "content_sha256": item.content_sha256,
                        "source_refs": item.source_refs,
                        "action": item.action,
                    }
                    for item in prepared
                ],
            }
        )
        generation_id = "projection-generation-" + hashlib.sha256(
            "\x1f".join((kind, str(scope), manifest_hash)).encode("utf-8")
        ).hexdigest()[:40]
        self._begin_generation(
            generation_id=generation_id,
            projection_kind=kind,
            scope_root=scope,
            manifest_hash=manifest_hash,
            full=full,
            items=prepared,
        )

        try:
            for item in prepared:
                if item.action == "delete":
                    self._publish_delete(generation_id, kind, item)
                else:
                    self._publish_upsert(generation_id, kind, item)
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            LookupError,
            sqlite3.Error,
        ) as exc:
            self._finish_generation(generation_id, status="failed", error=str(exc))
            raise
        self._finish_generation(generation_id, status="committed", error="")
        return self._generation_receipt(generation_id)

    def _validate_publisher_binding(self) -> None:
        """Fail closed when the event publisher and mutation ledger can diverge."""

        ledger_path = self.ledger.db_path.expanduser().resolve(strict=False)
        if self.event_bus is None:
            from core.wiki_projection_lifecycle import _default_db_path

            global_path = _default_db_path().expanduser().resolve(strict=False)
            if ledger_path != global_path:
                raise RuntimeError(
                    "a non-global Wiki projection ledger requires an explicit EventBus"
                )
            return
        bus_path = getattr(self.event_bus, "projection_db_path", None)
        if bus_path is not None and Path(bus_path).expanduser().resolve(
            strict=False
        ) != ledger_path:
            raise RuntimeError(
                "Wiki projection EventBus is bound to a different lifecycle ledger"
            )

    def _validated_scope(self, scope_root: Path | str) -> Path:
        scope = Path(scope_root).expanduser().resolve(strict=False)
        try:
            scope.relative_to(self.vault_dir)
        except ValueError as exc:
            raise ValueError("derived projection scope must be inside its vault") from exc
        return scope

    def _validated_target(self, path: Path, scope: Path) -> Path:
        target = path.expanduser().resolve(strict=False)
        try:
            relative = target.relative_to(scope)
        except ValueError as exc:
            raise ValueError("derived projection target must be inside its scope") from exc
        if target.suffix.lower() != ".md" or any(part.startswith(".") for part in relative.parts):
            raise ValueError("derived projection target must be visible Markdown")
        return target

    def _prepare_pages(
        self,
        projection_kind: str,
        scope: Path,
        pages: Sequence[ProjectionPageSpec],
    ) -> list[_PreparedItem]:
        prepared: list[_PreparedItem] = []
        seen: set[Path] = set()
        for page in pages:
            target = self._validated_target(Path(page.path), scope)
            if target in seen:
                raise ValueError(f"duplicate derived projection target: {target}")
            seen.add(target)
            role = str(page.page_role or "").strip()
            if not role.startswith(_PAGE_ROLE_PREFIXES):
                raise ValueError(f"unsupported derived projection page role: {role}")
            canonical_revision = str(page.canonical_revision or "").strip()
            if not canonical_revision:
                raise ValueError("derived projection requires a canonical revision")
            refs = tuple(str(ref).strip() for ref in page.source_refs if str(ref).strip())
            if not refs:
                raise ValueError("derived projection requires at least one canonical source ref")
            content = self._bind_frontmatter(
                page.content,
                projection_kind=projection_kind,
                page_role=role,
                canonical_revision=canonical_revision,
            )
            prepared.append(
                _PreparedItem(
                    path=target,
                    content=content,
                    page_role=role,
                    canonical_revision=canonical_revision,
                    source_refs=refs,
                    content_sha256=_sha256_text(content),
                    action="upsert",
                )
            )
        return prepared

    @staticmethod
    def _bind_frontmatter(
        content: str,
        *,
        projection_kind: str,
        page_role: str,
        canonical_revision: str,
    ) -> str:
        normalized = str(content)
        binding_lines = [
            f'projection_schema: "{DERIVED_PROJECTION_SCHEMA_VERSION}"',
            f'projection_kind: {json.dumps(projection_kind, ensure_ascii=False)}',
            f'page_role: {json.dumps(page_role, ensure_ascii=False)}',
            f'canonical_revision: {json.dumps(canonical_revision, ensure_ascii=False)}',
        ]
        if normalized.startswith("---\n"):
            end = normalized.find("\n---", 4)
            if end == -1:
                raise ValueError("projection Markdown has an unterminated frontmatter block")
            existing = normalized[4:end]
            forbidden = ("projection_schema:", "projection_kind:", "page_role:", "canonical_revision:")
            declared = [
                line.strip()
                for line in existing.splitlines()
                if line.strip().startswith(forbidden)
            ]
            if declared:
                if declared == binding_lines:
                    return normalized
                raise ValueError("projection renderer predeclared mismatched lifecycle bindings")
            return "---\n" + "\n".join(binding_lines) + "\n" + normalized[4:]
        body = normalized.lstrip("\n")
        return "---\n" + "\n".join(binding_lines) + "\n---\n\n" + body

    def bind_content(
        self,
        content: str,
        *,
        projection_kind: str,
        page_role: str,
        canonical_revision: str,
    ) -> str:
        """Return the exact bytes that the lifecycle will publish."""

        return self._bind_frontmatter(
            content,
            projection_kind=projection_kind,
            page_role=page_role,
            canonical_revision=canonical_revision,
        )

    def _full_generation_stale_paths(
        self,
        projection_kind: str,
        scope: Path,
        desired_paths: set[Path],
    ) -> set[Path]:
        candidates: set[Path] = set()
        if scope.is_dir():
            for path in scope.rglob("*.md"):
                relative = path.relative_to(scope)
                if not any(part.startswith(".") for part in relative.parts):
                    candidates.add(path.resolve(strict=False))
        candidates.update(self._active_manifest_paths(projection_kind, scope))
        return candidates - desired_paths

    def _active_manifest_paths(self, projection_kind: str, scope: Path) -> set[Path]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT item.target_path, item.action
                FROM derived_projection_generation_items AS item
                JOIN derived_projection_generations AS generation
                  ON generation.generation_id=item.generation_id
                WHERE generation.projection_kind=? AND generation.scope_root=?
                  AND item.rowid=(
                      SELECT newer.rowid
                      FROM derived_projection_generation_items AS newer
                      JOIN derived_projection_generations AS newer_generation
                        ON newer_generation.generation_id=newer.generation_id
                      WHERE newer.target_path=item.target_path
                        AND newer_generation.projection_kind=generation.projection_kind
                        AND newer_generation.scope_root=generation.scope_root
                      ORDER BY newer.updated_at DESC, newer.rowid DESC
                      LIMIT 1
                  )
                """,
                (projection_kind, str(scope)),
            ).fetchall()
        return {
            Path(str(row["target_path"]))
            for row in rows
            if str(row["action"]) == "upsert"
        }

    def _prepare_deletions(self, stale_paths: Iterable[Path]) -> list[_PreparedItem]:
        items: list[_PreparedItem] = []
        for path in sorted(stale_paths):
            binding = self.binding_for_path(path)
            identity = self.ledger.page_identity(path)
            canonical_revision = str(
                (binding or {}).get("canonical_revision")
                or (identity or {}).get("current_revision")
                or canonical_projection_revision({"legacy_stale_path": str(path)})
            )
            content_hash = str(
                (binding or {}).get("content_sha256")
                or (
                    "sha256:" + str((identity or {}).get("content_sha256"))
                    if (identity or {}).get("content_sha256")
                    else _sha256_file(path) if path.is_file() else _sha256_text("")
                )
            )
            items.append(
                _PreparedItem(
                    path=path,
                    content="",
                    page_role=str((binding or {}).get("page_role") or "derived_report:legacy"),
                    canonical_revision=canonical_revision,
                    source_refs=tuple((binding or {}).get("source_refs") or (f"stale:{path}",)),
                    content_sha256=content_hash,
                    action="delete",
                )
            )
        return items

    def _begin_generation(
        self,
        *,
        generation_id: str,
        projection_kind: str,
        scope_root: Path,
        manifest_hash: str,
        full: bool,
        items: Sequence[_PreparedItem],
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self._next_generation_timestamp(conn)
            conn.execute(
                """
                INSERT INTO derived_projection_generations (
                    generation_id, schema_version, projection_kind, scope_root,
                    manifest_hash, full_generation, expected_item_count,
                    status, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', '', ?, ?)
                ON CONFLICT(generation_id) DO UPDATE SET
                    status='running', error='', updated_at=excluded.updated_at
                """,
                (
                    generation_id,
                    DERIVED_PROJECTION_SCHEMA_VERSION,
                    projection_kind,
                    str(scope_root),
                    manifest_hash,
                    int(full),
                    len(items),
                    now,
                    now,
                ),
            )
            for item in items:
                conn.execute(
                    """
                    INSERT INTO derived_projection_generation_items (
                        generation_id, target_path, page_role, canonical_revision,
                        content_sha256, source_refs_json, action, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?)
                    ON CONFLICT(generation_id, target_path) DO UPDATE SET
                        page_role=excluded.page_role,
                        canonical_revision=excluded.canonical_revision,
                        content_sha256=excluded.content_sha256,
                        source_refs_json=excluded.source_refs_json,
                        action=excluded.action,
                        error='',
                        updated_at=excluded.updated_at
                    """,
                    (
                        generation_id,
                        str(item.path),
                        item.page_role,
                        item.canonical_revision,
                        item.content_sha256,
                        json.dumps(item.source_refs, ensure_ascii=False),
                        item.action,
                        now,
                    ),
                )
            conn.commit()

    @staticmethod
    def _next_generation_timestamp(conn: sqlite3.Connection) -> str:
        """Return a durable monotonic activation time for latest-binding queries."""

        current = datetime.now(timezone.utc)
        row = conn.execute(
            "SELECT MAX(updated_at) FROM derived_projection_generation_items"
        ).fetchone()
        latest_raw = str(row[0] or "") if row is not None else ""
        if latest_raw:
            try:
                latest = datetime.fromisoformat(latest_raw)
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=timezone.utc)
                if current <= latest:
                    current = latest + timedelta(microseconds=1)
            except ValueError:
                pass
        return current.isoformat()

    def _existing_item(self, generation_id: str, path: Path) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM derived_projection_generation_items
                   WHERE generation_id=? AND target_path=?""",
                (generation_id, str(path)),
            ).fetchone()
        return row if isinstance(row, sqlite3.Row) else None

    def _effect_authorization(
        self,
        generation_id: str,
        item: _PreparedItem,
        receipt: WikiMutationReceipt,
    ) -> DerivedProjectionMutationAuthorization:
        """Re-read and bind the exact manifest item to its durable mutation."""

        row = self._existing_item(generation_id, item.path)
        durable = self.ledger.mutation_receipt(receipt.mutation_id)
        expected_digest = item.content_sha256.split(":", 1)[-1]
        expected_delete = item.action == "delete"
        if (
            row is None
            or durable is None
            or str(row["target_path"]) != str(item.path)
            or str(row["content_sha256"]) != item.content_sha256
            or str(row["action"]) != item.action
            or str(row["mutation_id"]) != receipt.mutation_id
            or str(row["page_revision"]) != receipt.page_revision
            or durable.page_revision != receipt.page_revision
            or durable.page_path != str(item.path)
            or durable.content_sha256 != expected_digest
            or bool(durable.tombstone) != expected_delete
        ):
            raise RuntimeError("derived projection mutation authorization mismatch")
        return DerivedProjectionMutationAuthorization(
            generation_id=generation_id,
            mutation_id=receipt.mutation_id,
            page_revision=receipt.page_revision,
            target_path=str(item.path),
            content_sha256=item.content_sha256,
            action=item.action,
        )

    def _commit_authorized_upsert(
        self,
        generation_id: str,
        item: _PreparedItem,
        authorization: DerivedProjectionMutationAuthorization,
    ) -> None:
        try:
            item.path.parent.mkdir(parents=True, exist_ok=True)
            self._file_writer(authorization, item.path, item.content)
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            self._record_item_error(generation_id, item.path, str(exc))
            raise

    def _publish_upsert(
        self,
        generation_id: str,
        projection_kind: str,
        item: _PreparedItem,
    ) -> None:
        existing = self._existing_item(generation_id, item.path)
        if existing is not None and str(existing["mutation_id"]):
            receipt = self.ledger.mutation_receipt(str(existing["mutation_id"]))
            if receipt is not None:
                identity = self.ledger.page_identity(item.path)
                file_matches = bool(
                    item.path.is_file()
                    and _sha256_file(item.path) == item.content_sha256
                )
                receipt_is_current = bool(
                    identity
                    and str(identity["current_revision"]) == receipt.page_revision
                    and str(identity["lifecycle_state"]) == "active"
                )
                if receipt_is_current and file_matches:
                    self._publish_recorded(generation_id, projection_kind, item, receipt)
                    return
                if receipt_is_current and not receipt.event_trace_id:
                    authorization = self._effect_authorization(
                        generation_id,
                        item,
                        receipt,
                    )
                    self._commit_authorized_upsert(
                        generation_id,
                        item,
                        authorization,
                    )
                    self._publish_recorded(generation_id, projection_kind, item, receipt)
                    return

        identity = self.ledger.page_identity(item.path)
        file_matches = bool(
            item.path.is_file() and _sha256_file(item.path) == item.content_sha256
        )
        repaired_untracked_drift = bool(
            identity
            and str(identity["lifecycle_state"]) == "active"
            and str(identity["content_sha256"]) == item.content_sha256.split(":", 1)[1]
            and not file_matches
        )
        mutation_type = (
            "create"
            if identity is None or str(identity["lifecycle_state"]) == "tombstone"
            else "update"
        )
        receipt = self.ledger.record_mutation(
            item.path,
            mutation_type=mutation_type,
            force=repaired_untracked_drift,
            expected_content_sha256=item.content_sha256,
        )
        self._record_mutation(generation_id, item.path, receipt)
        if not file_matches:
            authorization = self._effect_authorization(generation_id, item, receipt)
            self._commit_authorized_upsert(
                generation_id,
                item,
                authorization,
            )
        self._publish_recorded(generation_id, projection_kind, item, receipt)

    def _publish_delete(
        self,
        generation_id: str,
        projection_kind: str,
        item: _PreparedItem,
    ) -> None:
        existing = self._existing_item(generation_id, item.path)
        receipt: WikiMutationReceipt | None = None
        if existing is not None and str(existing["mutation_id"]):
            receipt = self.ledger.mutation_receipt(str(existing["mutation_id"]))
            identity = self.ledger.page_identity(item.path)
            receipt_is_current = bool(
                receipt
                and identity
                and str(identity["current_revision"]) == receipt.page_revision
                and str(identity["lifecycle_state"]) == "tombstone"
            )
            if not receipt_is_current or (
                receipt is not None and receipt.event_trace_id and item.path.exists()
            ):
                receipt = None

        if receipt is None:
            identity = self.ledger.page_identity(item.path)
            if identity is None:
                if not item.path.is_file():
                    self._record_terminal_without_mutation(generation_id, item.path)
                    return
                adoption = self.ledger.record_mutation(item.path, mutation_type="create")
                from core.wiki_projection_publisher import publish_wiki_mutation

                publish_wiki_mutation(
                    adoption,
                    ledger=self.ledger,
                    source=f"derived_projection:{projection_kind}:legacy_adoption",
                    event_bus=self.event_bus,
                )
                identity = self.ledger.page_identity(item.path)
            force = bool(
                identity
                and str(identity["lifecycle_state"]) == "tombstone"
                and item.path.is_file()
            )
            receipt = self.ledger.record_mutation(
                item.path,
                mutation_type="delete",
                force=force,
            )
            self._record_mutation(generation_id, item.path, receipt)

        if item.path.exists():
            try:
                authorization = self._effect_authorization(
                    generation_id,
                    item,
                    receipt,
                )
                self._atomic_delete(authorization, item.path)
            except (OSError, RuntimeError, ValueError) as exc:
                self._record_item_error(generation_id, item.path, str(exc))
                raise
        self._publish_recorded(generation_id, projection_kind, item, receipt)

    def _publish_recorded(
        self,
        generation_id: str,
        projection_kind: str,
        item: _PreparedItem,
        receipt: WikiMutationReceipt,
    ) -> None:
        try:
            from core.wiki_projection_publisher import publish_wiki_mutation

            published = publish_wiki_mutation(
                receipt,
                ledger=self.ledger,
                source=f"derived_projection:{projection_kind}",
                event_bus=self.event_bus,
            )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            self._record_item_error(generation_id, item.path, str(exc))
            raise
        self._record_published(
            generation_id,
            item.path,
            event_trace_id=str(published.get("event_trace_id") or receipt.event_trace_id),
        )

    def _record_mutation(
        self,
        generation_id: str,
        path: Path,
        receipt: WikiMutationReceipt,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE derived_projection_generation_items
                SET status='mutation_recorded', mutation_id=?, page_revision=?,
                    event_trace_id=?, error='', updated_at=?
                WHERE generation_id=? AND target_path=?
                """,
                (
                    receipt.mutation_id,
                    receipt.page_revision,
                    receipt.event_trace_id,
                    _now(),
                    generation_id,
                    str(path),
                ),
            )
            conn.commit()

    def _record_published(
        self,
        generation_id: str,
        path: Path,
        *,
        event_trace_id: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE derived_projection_generation_items
                SET status='published', event_trace_id=?, error='', updated_at=?
                WHERE generation_id=? AND target_path=?
                """,
                (event_trace_id, _now(), generation_id, str(path)),
            )
            conn.commit()

    def _record_terminal_without_mutation(self, generation_id: str, path: Path) -> None:
        self._record_published(generation_id, path, event_trace_id="stale-noop")

    def _record_item_error(self, generation_id: str, path: Path, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE derived_projection_generation_items
                SET status='failed', error=?, updated_at=?
                WHERE generation_id=? AND target_path=?
                """,
                (str(error), _now(), generation_id, str(path)),
            )
            conn.commit()

    def _finish_generation(self, generation_id: str, *, status: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE derived_projection_generations
                SET status=?, error=?, updated_at=? WHERE generation_id=?
                """,
                (status, error, _now(), generation_id),
            )
            conn.commit()

    def _generation_receipt(self, generation_id: str) -> ProjectionGenerationReceipt:
        with self._connect() as conn:
            generation = conn.execute(
                "SELECT * FROM derived_projection_generations WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            rows = conn.execute(
                """
                SELECT * FROM derived_projection_generation_items
                WHERE generation_id=? ORDER BY target_path
                """,
                (generation_id,),
            ).fetchall()
        if generation is None:
            raise KeyError(f"unknown derived projection generation: {generation_id}")
        items = tuple(
            DerivedProjectionMutationReceipt(
                generation_id=generation_id,
                projection_kind=str(generation["projection_kind"]),
                path=str(row["target_path"]),
                page_role=str(row["page_role"]),
                canonical_revision=str(row["canonical_revision"]),
                content_sha256=str(row["content_sha256"]),
                action=str(row["action"]),
                status=str(row["status"]),
                mutation_id=str(row["mutation_id"]),
                page_revision=str(row["page_revision"]),
                event_trace_id=str(row["event_trace_id"]),
                error=str(row["error"]),
            )
            for row in rows
        )
        return ProjectionGenerationReceipt(
            generation_id=generation_id,
            projection_kind=str(generation["projection_kind"]),
            scope_root=str(generation["scope_root"]),
            manifest_hash=str(generation["manifest_hash"]),
            full_generation=bool(generation["full_generation"]),
            status=str(generation["status"]),
            expected_item_count=int(generation["expected_item_count"]),
            published_count=sum(item.status == "published" for item in items),
            items=items,
        )

    def binding_for_path(self, path: Path | str) -> dict[str, Any] | None:
        """Return the latest generation binding for one projection target."""

        target = str(Path(path).expanduser().resolve(strict=False))
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT item.*, generation.projection_kind, generation.scope_root,
                       generation.manifest_hash
                FROM derived_projection_generation_items AS item
                JOIN derived_projection_generations AS generation
                  ON generation.generation_id=item.generation_id
                WHERE item.target_path=?
                ORDER BY item.updated_at DESC, item.rowid DESC LIMIT 1
                """,
                (target,),
            ).fetchone()
        if row is None:
            return None
        try:
            refs = tuple(json.loads(str(row["source_refs_json"])))
        except (json.JSONDecodeError, TypeError, ValueError):
            refs = ()
        return {
            "generation_id": str(row["generation_id"]),
            "projection_kind": str(row["projection_kind"]),
            "scope_root": str(row["scope_root"]),
            "manifest_hash": str(row["manifest_hash"]),
            "path": str(row["target_path"]),
            "page_role": str(row["page_role"]),
            "canonical_revision": str(row["canonical_revision"]),
            "content_sha256": str(row["content_sha256"]),
            "source_refs": refs,
            "action": str(row["action"]),
            "status": str(row["status"]),
            "mutation_id": str(row["mutation_id"]),
            "page_revision": str(row["page_revision"]),
            "event_trace_id": str(row["event_trace_id"]),
            "error": str(row["error"]),
        }

    def stale_paths(
        self,
        *,
        projection_kind: str,
        scope_root: Path | str,
    ) -> list[str]:
        """Return manifest/filesystem mismatches for the latest target bindings."""

        scope = self._validated_scope(scope_root)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT item.*
                FROM derived_projection_generation_items AS item
                JOIN derived_projection_generations AS generation
                  ON generation.generation_id=item.generation_id
                WHERE generation.projection_kind=? AND generation.scope_root=?
                  AND item.rowid=(
                      SELECT newer.rowid
                      FROM derived_projection_generation_items AS newer
                      JOIN derived_projection_generations AS newer_generation
                        ON newer_generation.generation_id=newer.generation_id
                      WHERE newer.target_path=item.target_path
                        AND newer_generation.projection_kind=generation.projection_kind
                        AND newer_generation.scope_root=generation.scope_root
                      ORDER BY newer.updated_at DESC, newer.rowid DESC
                      LIMIT 1
                  )
                """,
                (str(projection_kind), str(scope)),
            ).fetchall()
        stale: list[str] = []
        for row in rows:
            path = Path(str(row["target_path"]))
            action = str(row["action"])
            status = str(row["status"])
            if status != "published":
                stale.append(str(path))
            elif action == "delete":
                if path.exists():
                    stale.append(str(path))
            elif not path.is_file() or _sha256_file(path) != str(
                row["content_sha256"]
            ):
                stale.append(str(path))
        return sorted(set(stale))
