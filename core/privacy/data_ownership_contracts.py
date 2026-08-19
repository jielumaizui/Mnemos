"""Data-ownership scope, inventory, request, and proof contracts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.db_utils import render_sql, validate_sql_identifier

DATA_OWNERSHIP_SCHEMA_VERSION = "mnemos.data_ownership.v1"
DATA_SCOPES = {
    "all",
    "agent",
    "session",
    "project",
    "path",
    "source",
    "time",
    "wiki_page",
    "persona_signal",
    "raw_event_id",
}
DATA_DOMAINS = {
    "raw",
    "wiki",
    "embedding_cache",
    "metadata",
    "evidence_refs",
    "persona",
    "reflection",
    "scoring",
    "action_ledger",
    "model_call_ledger",
    "consumer_access_log",
    "agent_source_metadata",
    "cognitive_state",
    "observation",
    "cognitive_graph",
}
DELETE_STATUSES = {
    "requested",
    "dry_run_planned",
    "frozen",
    "deleting",
    "deleted",
    "blocked",
    "partially_deleted",
    "verified",
}
SECRET_MARKERS = ("api_key", "token", "secret", "password", "authorization")
_MODEL_CALL_LEDGER_RETIRED_PROMPT_TABLES = frozenset(
    {"prompt_calls", "prompt_call_log", "prompt_call_stats"}
)
_MODEL_CALL_LEDGER_RETIRED_RECORD_TABLES = frozenset({"prompt_calls", "prompt_call_log"})
DATA_DELETE_DECISION_CONTRACT_ID = "project-contract:data-ownership-subject-delete"
DATA_DELETE_DECISION_CONTRACT_REVISION = "mnemos.data_ownership_material_delete.v1"
DATA_DELETE_DECISION_CONTRACT_TEXT = (
    "A confirmed data ownership request with an active freeze and a verified "
    "retained snapshot may delete only the exact object selected by its typed "
    "subject-deletion receipt."
)
DATA_DELETE_DECISION_PRODUCER_HASH = (
    "sha256:"
    + hashlib.sha256(
        json.dumps(
            {
                "module": "core.privacy.data_ownership",
                "producer": "DataOwnershipManager",
                "version": DATA_DELETE_DECISION_CONTRACT_REVISION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _deletion_operation_id(subject: "DataSubjectRef", snapshot_ref: str) -> str:
    """Return a stable, body-free identity for one confirmed delete attempt.

    Retries must address the same tombstones and propagation receipts.  A
    random identifier lets a retry see the already-hidden source as
    ``no_targets`` and bypass the first attempt's pending consumers.
    """

    payload = json.dumps(
        {
            "schema_version": DATA_OWNERSHIP_SCHEMA_VERSION,
            "scope_kind": subject.scope_kind,
            "scope_value_hash": _hash_text(subject.scope_value),
            "snapshot_ref_hash": _hash_text(str(snapshot_ref)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "delete-" + _hash_text(payload)[:40]


def _redact_persisted_request_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Persist ownership audit facts without retaining a subject or snapshot literal."""

    try:
        copied = json.loads(json.dumps(dict(payload), ensure_ascii=False, default=str))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("data ownership request payload is not serializable") from exc

    def scrub(value: Any) -> Any:
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if not isinstance(value, dict):
            return value
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            normalized_key = str(key)
            if normalized_key in {"scope_value", "snapshot_ref"}:
                redacted[normalized_key + "_hash"] = _hash_text(str(child))
                continue
            redacted[normalized_key] = scrub(child)
        return redacted

    result = scrub(copied)
    if not isinstance(result, dict):
        raise ValueError("data ownership request payload must be an object")
    return result


def _configured_path_value(config: Any, *names: str) -> Path | None:
    """Return only a concrete configured filesystem path.

    ``Mock`` objects manufacture arbitrary attributes.  Treating one as a
    path could point a privacy freeze at an unrelated location (or simply
    crash before the guard runs), so a configuration value is valid only when
    it is an actual path-like or string. Mapping-shaped historical configs are
    supported explicitly rather than through accidental attribute fallback.
    """

    for name in names:
        value = config.get(name) if isinstance(config, Mapping) else getattr(config, name, None)
        if isinstance(value, (str, os.PathLike)):
            return Path(value).expanduser()
    return None


def _mnemos_dir(config: Any) -> Path:
    return _configured_path_value(config, "mnemos_dir", "data_dir") or Path.home() / ".mnemos"


def _database_dir(config: Any) -> Path:
    return _configured_path_value(config, "database_dir") or _mnemos_dir(config)


def _configured_raw_event_db(config: Any) -> Path:
    """Resolve the canonical Raw owner without constructing or creating it."""

    configured = None
    get_value = getattr(config, "get", None)
    if callable(get_value):
        try:
            configured = get_value("raw_event_store.db_path", None)
        except (TypeError, ValueError):
            configured = None
    return Path(configured).expanduser() if configured else _database_dir(config) / "raw_events.db"


def _configured_reflection_db_paths(config: Any) -> tuple[Path, ...]:
    """Return the explicit Reflection owners without globbing user data paths."""

    candidates: list[Path] = []
    for attribute in ("reflection_db_path", "reflections_db_path"):
        value = getattr(config, attribute, None)
        if value:
            candidates.append(Path(value).expanduser())
    get_value = getattr(config, "get", None)
    if callable(get_value):
        for key in ("reflection.db_path", "reflections.db_path"):
            try:
                value = get_value(key, None)
            except (TypeError, ValueError):
                value = None
            if value:
                candidates.append(Path(value).expanduser())
    db_dir = _database_dir(config)
    candidates.extend(
        (
            db_dir / "reflections.db",
            db_dir / "reflection.db",  # historical inventory; do not silently skip it.
            _mnemos_dir(config) / "reflections.db",
        )
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate.expanduser())
        if normalized not in seen:
            seen.add(normalized)
            unique.append(Path(normalized))
    return tuple(unique)


def _configured_cognitive_graph_db_paths(config: Any) -> tuple[Path, ...]:
    """Return explicit CognitiveGraph owners without scanning arbitrary files."""

    candidates: list[Path] = []
    value = getattr(config, "cognitive_graph_db_path", None)
    if value:
        candidates.append(Path(value).expanduser())
    get_value = getattr(config, "get", None)
    if callable(get_value):
        try:
            value = get_value("cognitive_graph.db_path", None)
        except (TypeError, ValueError):
            value = None
        if value:
            candidates.append(Path(value).expanduser())
    candidates.extend(
        (
            _database_dir(config) / "cognitive_graph.db",
            _mnemos_dir(config) / "cognitive_graph.db",
        )
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate.expanduser())
        if normalized not in seen:
            seen.add(normalized)
            unique.append(Path(normalized))
    return tuple(unique)


def _configured_observation_db_paths(config: Any) -> tuple[Path, ...]:
    """Return explicit Observation owners without scanning arbitrary paths."""

    candidates: list[Path] = []
    configured = _configured_path_value(config, "observation_db_path", "observations_db_path")
    if configured is not None:
        candidates.append(configured)
    get_value = getattr(config, "get", None)
    if callable(get_value):
        for key in ("observation.db_path", "observations.db_path"):
            try:
                value = get_value(key, None)
            except (TypeError, ValueError):
                value = None
            if isinstance(value, (str, os.PathLike)):
                candidates.append(Path(value).expanduser())
    candidates.extend(
        (
            _database_dir(config) / "observations.db",
            _mnemos_dir(config) / "observations.db",
        )
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate.expanduser().resolve(strict=False))
        if normalized not in seen:
            seen.add(normalized)
            unique.append(Path(normalized))
    return tuple(unique)


def _configured_wiki_projection_db(config: Any) -> Path:
    """Resolve the canonical Wiki lifecycle owner without creating it."""

    from core.wiki_projection_lifecycle import resolve_wiki_projection_db_path

    return resolve_wiki_projection_db_path(config)


def _configured_wiki_metrics_db(config: Any) -> Path:
    """Resolve the required ``wiki_metrics`` lifecycle consumer store."""

    for attribute in ("wiki_metrics_db_path",):
        value = getattr(config, attribute, None)
        if value:
            return Path(value).expanduser()
    get_value = getattr(config, "get", None)
    if callable(get_value):
        try:
            value = get_value("wiki_metrics.db_path", None)
        except (TypeError, ValueError):
            value = None
        if value:
            return Path(value).expanduser()
    return _database_dir(config) / "wiki_metrics.db"


def _configured_embedding_cache_db(config: Any) -> Path:
    """Resolve the cache owner without constructing or initializing it."""

    return _database_dir(config) / "embedding_cache.db"


def _configured_event_bus_db(config: Any) -> Path:
    """Resolve EventBus' canonical database path without constructing a bus."""

    from core.mnemos_bus import _resolve_event_db_dir

    return _resolve_event_db_dir(config) / "events.db"


def _configured_scoring_db_paths(config: Any) -> tuple[Path, ...]:
    """Return each declared persistent scoring owner without directory scans."""

    db_dir = _database_dir(config)
    candidates = (
        db_dir / "mnemos.db",
        db_dir / "bayesian_scorer.db",
        db_dir / "feedback_channel.db",
    )
    return tuple(dict.fromkeys(candidates))


def _configured_persona_db_paths(config: Any) -> tuple[Path, ...]:
    """Return declared profile stores without globbing arbitrary user data."""

    candidates: list[Path] = []
    for attribute in ("persona_db_path", "user_signals_db_path"):
        value = getattr(config, attribute, None)
        if value:
            candidates.append(Path(value).expanduser())
    get_value = getattr(config, "get", None)
    if callable(get_value):
        for key in ("persona.db_path", "user_signals.db_path"):
            try:
                value = get_value(key, None)
            except (TypeError, ValueError):
                value = None
            if value:
                candidates.append(Path(value).expanduser())
    db_dir = _database_dir(config)
    candidates.extend(
        (
            db_dir / "user_signals.db",
            db_dir / "persona.db",  # explicit historical owner; never silently skip it.
            _mnemos_dir(config) / "user_signals.db",
        )
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate.expanduser())
        if normalized not in seen:
            seen.add(normalized)
            unique.append(Path(normalized))
    return tuple(unique)


def _vault_dir(config: Any, name: str) -> Path | None:
    method = getattr(config, "vault_dir", None)
    if callable(method):
        try:
            return Path(method(name))
        except (KeyError, TypeError, ValueError):
            return None
    if name == "mnemos" and hasattr(config, "wiki_dir"):
        return Path(config.wiki_dir)
    if name == "raw" and hasattr(config, "obsidian_vault_path"):
        return Path(config.obsidian_vault_path)
    return None


def _count_sqlite_rows(db_path: Path, table: str) -> int:
    if not db_path.exists():
        return 0
    try:
        table_name = validate_sql_identifier(table)
        with sqlite3.connect(
            db_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=2,
        ) as conn:
            query = f"SELECT COUNT(*) FROM {table_name}"  # nosec B608
            row = conn.execute(query).fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.Error, ValueError):
        return 0


def _canonical_model_call_ledger_retired_prompt_storage_count(db_path: Path) -> int:
    """Return a fail-closed count for retired prompt storage in the owner DB.

    A canonical ledger containing a retired prompt table cannot be treated as
    empty merely because ``model_call_entries`` has no rows. Record tables
    count their rows; an empty record table and the aggregate-only stats table
    each still count as one blocked residual, because their presence requires
    backup-gated model-call reconciliation. This reads only table names and
    row counts, never prompt cells.
    """
    if not db_path.is_file():
        return 0
    try:
        with sqlite3.connect(
            db_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=2,
        ) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            blocked = 0
            for table in sorted(tables & _MODEL_CALL_LEDGER_RETIRED_PROMPT_TABLES):
                if table in _MODEL_CALL_LEDGER_RETIRED_RECORD_TABLES:
                    table_name = validate_sql_identifier(table)
                    row = conn.execute(
                        render_sql(
                            "SELECT COUNT(*) FROM {table}",
                            identifiers={"table": table_name},
                        )
                    ).fetchone()
                    blocked += max(1, int(row[0] or 0) if row else 0)
                else:
                    # Aggregate stats cannot establish one-call provenance.
                    blocked += 1
            return blocked
    except (sqlite3.Error, OSError, ValueError):
        # An unreadable canonical owner is a migration/privacy blocker, never
        # evidence that it contains no retired prompt storage.
        return 1


def parse_scope(scope: str) -> tuple[str, str]:
    if scope == "all":
        return "all", "all"
    if ":" not in scope:
        raise ValueError("scope must be 'all' or '<scope_kind>:<value>'")
    kind, value = scope.split(":", 1)
    if kind not in DATA_SCOPES or kind == "all":
        raise ValueError(f"unsupported data ownership scope: {kind}")
    if not value.strip():
        raise ValueError("scope value is required")
    return kind, value.strip()


@dataclass(frozen=True)
class DataSubjectRef:
    scope_kind: str
    scope_value: str

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.scope_kind not in DATA_SCOPES:
            errors.append(f"unknown scope_kind: {self.scope_kind}")
        if not self.scope_value:
            errors.append("scope_value is required")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataDomainInventory:
    domain: str
    storage_refs: tuple[str, ...]
    estimated_records: int
    consumer_ids: tuple[str, ...]
    export_policy: str
    freeze_policy: str
    delete_policy: str

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.domain not in DATA_DOMAINS:
            errors.append(f"unknown data domain: {self.domain}")
        if not self.storage_refs:
            errors.append(f"{self.domain}: storage_refs required")
        if not self.consumer_ids:
            errors.append(f"{self.domain}: consumer_ids required")
        if not self.export_policy or not self.freeze_policy or not self.delete_policy:
            errors.append(f"{self.domain}: export/freeze/delete policies required")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataExportManifest:
    export_id: str
    subject: DataSubjectRef
    generated_at: str
    dry_run: bool
    domains: tuple[DataDomainInventory, ...]
    output_path: str
    redaction_policy: str
    action_ledger_ref: str
    schema_version: str = DATA_OWNERSHIP_SCHEMA_VERSION

    def validate(self) -> list[str]:
        errors = self.subject.validate()
        if not self.export_id:
            errors.append("export_id is required")
        if not self.domains:
            errors.append("domains required")
        for domain in self.domains:
            errors.extend(domain.validate())
        if self.redaction_policy != "secret_redacted_summary":
            errors.append("redaction_policy must be secret_redacted_summary")
        text = json.dumps(self.as_dict(), ensure_ascii=False).lower()
        if any(marker in text for marker in SECRET_MARKERS) and "secret_redacted" not in text:
            errors.append("manifest contains secret-like marker without redaction policy")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataFreezeRequest:
    request_id: str
    subject: DataSubjectRef
    status: str
    created_at: str
    affected_domains: tuple[str, ...]
    reason: str
    action_ledger_ref: str
    schema_version: str = DATA_OWNERSHIP_SCHEMA_VERSION

    def validate(self) -> list[str]:
        errors = self.subject.validate()
        if self.status not in DELETE_STATUSES:
            errors.append(f"unknown freeze status: {self.status}")
        if self.status != "frozen":
            errors.append("freeze request must use frozen status after creation")
        if not self.affected_domains:
            errors.append("affected_domains required")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataDeleteRequest:
    request_id: str
    subject: DataSubjectRef
    status: str
    created_at: str
    dry_run: bool
    affected_domains: tuple[str, ...]
    derived_impacts: tuple[str, ...]
    requires_freeze: bool
    requires_snapshot: bool
    requires_confirmation: bool
    snapshot_ref: str = ""
    action_ledger_ref: str = ""
    schema_version: str = DATA_OWNERSHIP_SCHEMA_VERSION

    def validate(self) -> list[str]:
        errors = self.subject.validate()
        if self.status not in DELETE_STATUSES:
            errors.append(f"unknown delete status: {self.status}")
        if not self.affected_domains:
            errors.append("affected_domains required")
        if not self.derived_impacts:
            errors.append("derived_impacts required")
        if not self.dry_run and self.requires_snapshot and not self.snapshot_ref:
            errors.append("delete apply requires snapshot_ref")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DeletionProof:
    proof_id: str
    subject_hash: str
    status: str
    deleted_at: str
    affected_domains: tuple[str, ...]
    affected_consumers: tuple[str, ...]
    verification_results: Mapping[str, Any] = field(default_factory=dict)
    redaction_policy: str = "no_secret_no_pii"
    schema_version: str = DATA_OWNERSHIP_SCHEMA_VERSION

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.proof_id:
            errors.append("proof_id is required")
        if self.status not in DELETE_STATUSES:
            errors.append(f"unknown deletion proof status: {self.status}")
        if not self.subject_hash or len(self.subject_hash) < 16:
            errors.append("subject_hash required")
        if not self.affected_domains:
            errors.append("affected_domains required")
        if not self.affected_consumers:
            errors.append("affected_consumers required")
        payload = self.as_dict()
        payload.pop("redaction_policy", None)
        text = json.dumps(payload, ensure_ascii=False).lower()
        if any(marker in text for marker in SECRET_MARKERS):
            errors.append("deletion proof must not contain secret-like values")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
