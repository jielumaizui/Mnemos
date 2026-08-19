"""Canonical execution boundary for audits that inspect formal state.

Synthetic audit work owns one hermetic root and receives every writable
dependency explicitly.  Production inspection is represented by a read-only
snapshot boundary: targets are never initialized and any signature change
fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import stat as stat_module
import sqlite3
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from core.mnemos_bus import EventBus
from core.ops.hermetic_run import (
    SCHEMA_VERSION as HERMETIC_RUN_SCHEMA_VERSION,
    HermeticRunEnvironment,
    bound_process_run_identity,
    path_signature,
    verify_environment_manifest,
)
from core.runtime_environment import environment_get, environment_snapshot
from core.utils import load_json_value, read_bytes_value
from core.wiki_derived_projection import DerivedProjectionLifecycle
from core.wiki_projection_lifecycle import WikiProjectionLedger

AUDIT_RUN_SCHEMA_VERSION = "mnemos.audit_execution_environment.v1"
AUDIT_EVIDENCE_EPOCH_SCHEMA_VERSION = "mnemos.audit_evidence_epoch.v1"
logger = logging.getLogger(__name__)
_HERMETIC_OWNED_ENV_KEYS = (
    "HOME",
    "USERPROFILE",
    "MNEMOS_DIR",
    "MNEMOS_DATABASE_DIR",
    "MNEMOS_WIKI_DIR",
    "MNEMOS_OBSIDIAN_CONFIG_PATH",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "PYTHONPYCACHEPREFIX",
    "MNEMOS_RUN_ARTIFACTS_DIR",
)
_HERMETIC_TEST_OVERRIDE_KEYS = frozenset({"MNEMOS_OBSIDIAN_CONFIG_PATH"})


def _resolved(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _validated_hermetic_test_root() -> Path | None:
    """Return a structurally valid root for sandbox-confined test reads.

    The manifest is self-checking rather than a trust credential. Safety comes
    from confining every sandbox target to this root; production reads never
    consume the test boundary.
    """

    if (
        not environment_get("PYTEST_CURRENT_TEST", "")
        or environment_get("MNEMOS_TEST_RUN", "") != "1"
        or environment_get("MNEMOS_RUN_PROFILE", "") != "isolated"
    ):
        return None
    binding = bound_process_run_identity()
    if binding is None:
        return None
    bound_root, bound_manifest, bound_environment_hash = binding
    root_value = environment_get("MNEMOS_RUN_ROOT", "")
    manifest_value = environment_get("MNEMOS_RUN_ENVIRONMENT_MANIFEST", "")
    environment_hash = environment_get("MNEMOS_RUN_ENVIRONMENT_HASH", "")
    if (
        not root_value
        or not manifest_value
        or len(environment_hash) != 64
        or any(character not in "0123456789abcdef" for character in environment_hash)
    ):
        return None
    raw_root = Path(root_value)
    raw_manifest = Path(manifest_value)
    if raw_root.is_symlink() or raw_manifest.is_symlink():
        return None
    root = _resolved(raw_root)
    manifest = _resolved(raw_manifest)
    if (
        (root, manifest, environment_hash) != (bound_root, bound_manifest, bound_environment_hash)
        or not root.is_dir()
        or manifest != root / "artifacts" / "environment-manifest.json"
        or not manifest.is_file()
    ):
        return None
    try:
        payload = load_json_value(manifest)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != HERMETIC_RUN_SCHEMA_VERSION
        or payload.get("profile") != "isolated"
        or _resolved(str(payload.get("sandbox_root", ""))) != root
        or _resolved(str(payload.get("manifest_path", ""))) != manifest
        or payload.get("environment_hash") != environment_hash
        or payload.get("outside_write_count") != 0
        or payload.get("formal_state_diff") != []
        or not verify_environment_manifest(payload, environment_snapshot())
    ):
        return None
    owned_paths = payload.get("owned_paths")
    if not isinstance(owned_paths, dict):
        return None
    for key in _HERMETIC_OWNED_ENV_KEYS:
        manifest_owned_value = owned_paths.get(key)
        if not isinstance(manifest_owned_value, str) or not manifest_owned_value:
            return None
        manifest_owned_path = _resolved(manifest_owned_value)
        if manifest_owned_path != root and root not in manifest_owned_path.parents:
            return None
        current_value = environment_get(key, "")
        if not current_value:
            return None
        if key in _HERMETIC_TEST_OVERRIDE_KEYS:
            current_path = _resolved(current_value)
            if current_path != root and root not in current_path.parents:
                return None
        elif current_value != manifest_owned_value:
            return None
    return root


def verify_os_write_denied(
    probe: Path,
    *,
    expected_device: int,
    expected_inode: int,
    expected_sha256: str,
) -> str:
    """Prove an owner-writable, identity-bound sentinel is OS write-denied."""

    candidate = _resolved(probe)
    if not candidate.is_file():
        raise RuntimeError("OS write-deny probe must be an existing file")
    candidate_stat = candidate.stat()
    get_effective_uid = getattr(os, "geteuid", None)
    if not callable(get_effective_uid):
        raise RuntimeError("OS write-deny owner identity is unavailable on this platform")
    if (
        candidate_stat.st_dev != expected_device
        or candidate_stat.st_ino != expected_inode
        or candidate_stat.st_uid != get_effective_uid()
        or not stat_module.S_ISREG(candidate_stat.st_mode)
        or not candidate_stat.st_mode & stat_module.S_IWUSR
        or candidate_stat.st_nlink != 1
    ):
        raise RuntimeError("OS write-deny sentinel identity or writable mode mismatch")
    digest = hashlib.sha256(read_bytes_value(candidate)).hexdigest()
    if digest != expected_sha256:
        raise RuntimeError("OS write-deny sentinel content hash mismatch")
    try:
        descriptor = os.open(candidate, os.O_WRONLY)
    except PermissionError:
        return "sandbox-exec-v1"
    except OSError as exc:
        raise RuntimeError(f"OS write-deny probe was inconclusive: {exc}") from exc
    else:
        os.close(descriptor)
        raise RuntimeError("sandbox-exec OS write guard is not enforced")


def discover_audit_formal_state_targets(
    environment: Mapping[str, str],
) -> tuple[Path, ...]:
    """Return high-risk formal files whose bytes must remain unchanged."""

    home = _resolved(environment.get("HOME") or Path.home())
    mnemos = _resolved(environment.get("MNEMOS_DIR") or home / ".mnemos")
    database = _resolved(environment.get("MNEMOS_DATABASE_DIR") or mnemos)
    targets = set(audit_database_state_targets(database))
    targets.add(mnemos / "configs" / "main.json")
    obsidian = environment.get("MNEMOS_OBSIDIAN_CONFIG_PATH")
    if obsidian:
        targets.add(_resolved(obsidian))
    return tuple(sorted(targets))


def audit_database_state_targets(database: Path) -> tuple[Path, ...]:
    """Return exact DB/WAL/SHM targets for one configured database root."""

    database = _resolved(database)
    database_names = (
        "events.db",
        "observations.db",
        "producer_consumer_ledger.db",
        "wiki_projection.db",
    )
    return tuple(
        sorted(
            {
                database / suffix
                for name in database_names
                for suffix in (name, f"{name}-wal", f"{name}-shm")
            }
        )
    )


def discover_audit_formal_directory_targets(
    environment: Mapping[str, str],
) -> tuple[Path, ...]:
    """Return formal trees whose names and file metadata must not change."""

    home = _resolved(environment.get("HOME") or Path.home())
    mnemos = _resolved(environment.get("MNEMOS_DIR") or home / ".mnemos")
    database = _resolved(environment.get("MNEMOS_DATABASE_DIR") or mnemos)
    wiki = _resolved(environment.get("MNEMOS_WIKI_DIR") or home / "MnemosWiki")
    return tuple(
        sorted(
            {
                mnemos,
                database,
                mnemos / "raw",
                database / "logs",
                wiki,
                home / "Desktop" / "mnemos系统图谱",
            }
        )
    )


def directory_structure_signature(path: Path) -> dict[str, object]:
    """Hash tree names and metadata without rereading every file body."""

    try:
        root_stat = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if not path.is_dir() or path.is_symlink():
        return {
            "exists": True,
            "kind": "not_directory",
            "target_signature": path_signature(path),
        }
    entries: list[tuple[str, str, int, int, int, int, str]] = []
    for current_root, directories, files in os.walk(path):
        current = Path(current_root)
        directories.sort()
        files.sort()
        for name in directories:
            child = current / name
            relative = str(child.relative_to(path))
            if child.is_symlink():
                stat = child.lstat()
                entries.append(
                    (
                        relative,
                        "symlink",
                        0,
                        stat.st_ino,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                        os.readlink(child),
                    )
                )
            else:
                stat = child.stat()
                entries.append(
                    (
                        relative,
                        "dir",
                        0,
                        stat.st_ino,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                        "",
                    )
                )
        for name in files:
            child = current / name
            relative = str(child.relative_to(path))
            if child.is_symlink():
                stat = child.lstat()
                entries.append(
                    (
                        relative,
                        "symlink",
                        0,
                        stat.st_ino,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                        os.readlink(child),
                    )
                )
            else:
                stat = child.stat()
                entries.append(
                    (
                        relative,
                        "file",
                        stat.st_size,
                        stat.st_ino,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                        "",
                    )
                )
    payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    return {
        "exists": True,
        "kind": "directory_structure",
        "entry_count": len(entries),
        "tree_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "inode": root_stat.st_ino,
        "mtime_ns": root_stat.st_mtime_ns,
        "ctime_ns": root_stat.st_ctime_ns,
    }


@dataclass(frozen=True)
class AuditRuntimeConfig:
    """Small explicit config understood by WikiProjectionLedger/EventBus."""

    mnemos_dir: Path
    database_dir: Path
    data_dir: Path
    wiki_dir: Path
    values: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@dataclass(frozen=True)
class AuditEvidenceEpoch:
    """Hash-bound immutable SQLite snapshots captured under one quiesced cutoff."""

    epoch_id: str
    common_cutoff: str
    database_root: Path
    source_inventory_hash: str
    database_snapshots: tuple[Mapping[str, object], ...]
    writer_quiescence_checks: int
    _snapshot_paths: Mapping[Path, Path] = field(repr=False, compare=False)

    @classmethod
    def capture(
        cls,
        databases: Sequence[Path],
        *,
        snapshot_root: Path,
        formal_before: Mapping[str, Mapping[str, object]],
        writer_inactive: Callable[[Path], bool],
    ) -> "AuditEvidenceEpoch":
        """Capture verified backups only while all known Mnemos writers are inactive."""

        sources = tuple(sorted({_resolved(path) for path in databases}))
        if len(sources) < 2:
            raise ValueError("an evidence epoch requires at least two SQLite databases")
        database_roots = {source.parent for source in sources}
        if len(database_roots) != 1:
            raise ValueError("an evidence epoch requires one common database root")
        database_root = next(iter(database_roots))
        if not writer_inactive(database_root):
            raise RuntimeError("multi-database evidence epoch requires inactive runtime writers")

        source_targets = tuple(
            path
            for source in sources
            for path in (
                source,
                source.with_name(source.name + "-wal"),
                source.with_name(source.name + "-shm"),
            )
        )
        source_inventory = {str(path): path_signature(path) for path in source_targets}
        if any(
            formal_before.get(path) != signature for path, signature in source_inventory.items()
        ):
            raise RuntimeError("formal SQLite inventory changed before evidence epoch capture")

        root = _resolved(snapshot_root)
        if root.exists():
            if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
                raise FileExistsError("evidence snapshot root must be a unique empty directory")
        else:
            root.mkdir(mode=0o700, parents=True)
        os.chmod(root, 0o700)

        snapshot_paths: dict[Path, Path] = {}
        snapshot_receipts: list[Mapping[str, object]] = []

        def discard_snapshots(*extra_targets: Path) -> None:
            for snapshot in {*snapshot_paths.values(), *extra_targets}:
                # trusted-scan: artifact owner=ops target=epoch_snapshot expires=never ephemeral
                snapshot.unlink(missing_ok=True)

        for source in sources:
            if not source.is_file():
                snapshot_receipts.append(
                    MappingProxyType(
                        {
                            "source": str(source),
                            "existed": False,
                            "sha256": "",
                            "integrity_check": "uninitialized",
                        }
                    )
                )
                continue
            target = root / source.name
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            try:
                with sqlite3.connect(
                    f"{source.resolve(strict=True).as_uri()}?mode=ro",
                    uri=True,
                ) as source_connection:
                    with sqlite3.connect(target) as snapshot_connection:
                        source_connection.backup(snapshot_connection)
                        integrity = str(
                            snapshot_connection.execute("PRAGMA integrity_check").fetchone()[0]
                        )
                if integrity != "ok":
                    raise RuntimeError("evidence epoch SQLite snapshot integrity check failed")
                os.chmod(target, 0o600)
                digest = _sha256_file(target)
                target_stat = target.lstat()
            except BaseException:
                discard_snapshots(target)
                raise
            snapshot_paths[source] = target
            snapshot_receipts.append(
                MappingProxyType(
                    {
                        "source": str(source),
                        "existed": True,
                        "sha256": digest,
                        "integrity_check": integrity,
                        "device": target_stat.st_dev,
                        "inode": target_stat.st_ino,
                        "size": target_stat.st_size,
                    }
                )
            )

        try:
            if not writer_inactive(database_root):
                raise RuntimeError("runtime writer became active during evidence epoch capture")
            after_inventory = {str(path): path_signature(path) for path in source_targets}
            if source_inventory != after_inventory:
                raise RuntimeError("formal SQLite inventory changed during evidence epoch capture")
        except BaseException:
            discard_snapshots()
            raise

        cutoff = datetime.now(timezone.utc).isoformat()
        inventory_hash = (
            "sha256:"
            + hashlib.sha256(_canonical_json(source_inventory).encode("utf-8")).hexdigest()
        )
        identity_payload = {
            "schema_version": AUDIT_EVIDENCE_EPOCH_SCHEMA_VERSION,
            "common_cutoff": cutoff,
            "database_root": str(database_root),
            "source_inventory_hash": inventory_hash,
            "database_snapshots": [dict(item) for item in snapshot_receipts],
            "writer_quiescence_checks": 2,
        }
        epoch_id = (
            "sha256:"
            + hashlib.sha256(_canonical_json(identity_payload).encode("utf-8")).hexdigest()
        )
        return cls(
            epoch_id=epoch_id,
            common_cutoff=cutoff,
            database_root=database_root,
            source_inventory_hash=inventory_hash,
            database_snapshots=tuple(snapshot_receipts),
            writer_quiescence_checks=2,
            _snapshot_paths=MappingProxyType(snapshot_paths),
        )

    def snapshot_for(self, source: Path) -> Path | None:
        """Return the immutable snapshot bound to one source database."""

        return self._snapshot_paths.get(_resolved(source))

    def report(self) -> dict[str, object]:
        """Return the durable, path-safe epoch evidence stored by the audit."""

        return {
            "schema_version": AUDIT_EVIDENCE_EPOCH_SCHEMA_VERSION,
            "epoch_id": self.epoch_id,
            "common_cutoff": self.common_cutoff,
            "database_root": str(self.database_root),
            "database_count": len(self.database_snapshots),
            "source_inventory_hash": self.source_inventory_hash,
            "database_snapshots": [dict(item) for item in self.database_snapshots],
            "writer_quiescence_checks": self.writer_quiescence_checks,
        }


@dataclass
class AuditExecutionEnvironment:
    """Isolated writable audit run or fail-closed production read boundary."""

    mode: str
    root: Path | None
    formal_targets: tuple[Path, ...]
    formal_directory_targets: tuple[Path, ...]
    _before: Mapping[str, Mapping[str, object]]
    _directory_before: Mapping[str, Mapping[str, object]]
    _hermetic: HermeticRunEnvironment | None = field(default=None, repr=False)
    _event_buses: list[EventBus] = field(default_factory=list, repr=False)
    _closed: bool = field(default=False, repr=False)
    _cached_diff: tuple[str, ...] | None = field(default=None, repr=False)
    os_write_guard: str = ""
    evidence_epoch: AuditEvidenceEpoch | None = None

    @classmethod
    def isolated(
        cls,
        root: Path,
        *,
        base_environment: Mapping[str, str] | None = None,
        formal_targets: Sequence[Path] | None = None,
        formal_directory_targets: Sequence[Path] | None = None,
    ) -> "AuditExecutionEnvironment":
        base = environment_snapshot() if base_environment is None else dict(base_environment)
        targets = tuple(
            formal_targets
            if formal_targets is not None
            else discover_audit_formal_state_targets(base)
        )
        before = {str(path): path_signature(path) for path in targets}
        directory_targets = tuple(
            formal_directory_targets
            if formal_directory_targets is not None
            else discover_audit_formal_directory_targets(base)
        )
        directory_before = {
            str(path): directory_structure_signature(path) for path in directory_targets
        }
        hermetic = HermeticRunEnvironment.create(
            root,
            base_environment=base,
            formal_targets=targets,
        )
        return cls(
            mode="isolated",
            root=hermetic.root,
            formal_targets=targets,
            formal_directory_targets=directory_targets,
            _before=MappingProxyType(before),
            _directory_before=MappingProxyType(directory_before),
            _hermetic=hermetic,
            os_write_guard=environment_get("MNEMOS_AUDIT_OS_WRITE_DENY", ""),
        )

    @classmethod
    def production_readonly(
        cls,
        targets: Sequence[Path] = (),
        *,
        directory_targets: Sequence[Path] = (),
        required_sqlite_databases: Sequence[Path] = (),
        evidence_snapshot_root: Path | None = None,
        write_deny_probe: Path | None = None,
        write_deny_identity: Mapping[str, object] | None = None,
    ) -> "AuditExecutionEnvironment":
        """Open a production boundary only after proving OS-level write denial."""

        if write_deny_probe is None or write_deny_identity is None:
            raise RuntimeError("production read-only audit requires a verified OS write-deny probe")
        guard = verify_os_write_denied(
            write_deny_probe,
            expected_device=int(str(write_deny_identity["device"])),
            expected_inode=int(str(write_deny_identity["inode"])),
            expected_sha256=str(write_deny_identity["sha256"]),
        )
        return cls._readonly_boundary(
            mode="production_readonly",
            root=None,
            guard=guard,
            targets=targets,
            directory_targets=directory_targets,
            required_sqlite_databases=required_sqlite_databases,
            evidence_snapshot_root=evidence_snapshot_root,
            writer_inactive_override=None,
        )

    @classmethod
    def sandbox_readonly(
        cls,
        targets: Sequence[Path] = (),
        *,
        directory_targets: Sequence[Path] = (),
        required_sqlite_databases: Sequence[Path] = (),
        evidence_snapshot_root: Path | None = None,
        writer_inactive: Callable[[Path], bool] | None = None,
    ) -> "AuditExecutionEnvironment":
        """Open a test-only reader whose complete path set is run-root confined."""

        root = _validated_hermetic_test_root()
        if root is None:
            raise RuntimeError("sandbox read boundary requires a valid hermetic test environment")
        candidates = (
            *(_resolved(path) for path in targets),
            *(_resolved(path) for path in directory_targets),
            *(_resolved(path) for path in required_sqlite_databases),
            *((_resolved(evidence_snapshot_root),) if evidence_snapshot_root is not None else ()),
        )
        escaped = tuple(path for path in candidates if path != root and root not in path.parents)
        if escaped:
            raise RuntimeError(
                "sandbox read boundary target escapes hermetic run root: "
                + ", ".join(str(path) for path in escaped)
            )
        return cls._readonly_boundary(
            mode="sandbox_readonly",
            root=root,
            guard="hermetic-sandbox-confinement",
            targets=targets,
            directory_targets=directory_targets,
            required_sqlite_databases=required_sqlite_databases,
            evidence_snapshot_root=evidence_snapshot_root,
            writer_inactive_override=writer_inactive,
        )

    @classmethod
    def _readonly_boundary(
        cls,
        *,
        mode: str,
        root: Path | None,
        guard: str,
        targets: Sequence[Path],
        directory_targets: Sequence[Path],
        required_sqlite_databases: Sequence[Path],
        evidence_snapshot_root: Path | None,
        writer_inactive_override: Callable[[Path], bool] | None,
    ) -> "AuditExecutionEnvironment":
        resolved = tuple(sorted({_resolved(path) for path in targets}))
        resolved_directories = tuple(sorted({_resolved(path) for path in directory_targets}))
        if not resolved and not resolved_directories:
            raise ValueError("read-only audit requires explicit targets")
        required_databases = tuple(sorted({_resolved(path) for path in required_sqlite_databases}))
        if len(required_databases) > 1:
            required_group = {
                path
                for database in required_databases
                for path in (
                    database,
                    database.with_name(database.name + "-wal"),
                    database.with_name(database.name + "-shm"),
                )
            }
            required_directories = {database.parent for database in required_databases}
            if not required_group <= set(resolved) or not required_directories <= set(
                resolved_directories
            ):
                raise ValueError(
                    "multi-database evidence epoch requires a complete DB/WAL/SHM target group "
                    "and every database parent directory"
                )
            if evidence_snapshot_root is None:
                raise ValueError("multi-database evidence epoch requires a snapshot root")
            if writer_inactive_override is not None and mode != "sandbox_readonly":
                raise ValueError("writer override requires the sandbox read boundary")
        before = {str(path): path_signature(path) for path in resolved}
        directory_before = {
            str(path): directory_structure_signature(path) for path in resolved_directories
        }
        evidence_epoch = None
        if len(required_databases) > 1:
            if evidence_snapshot_root is None:
                raise ValueError("multi-database evidence epoch requires a snapshot root")
            snapshot_root = _resolved(evidence_snapshot_root)
            if any(
                snapshot_root == path
                or path in snapshot_root.parents
                or snapshot_root in path.parents
                for path in (*resolved, *resolved_directories)
            ):
                raise ValueError("evidence snapshot root must be outside every formal target")
            if writer_inactive_override is not None:
                writer_inactive = writer_inactive_override
            else:
                from core.migrations.model_call_ledger_reconcile.runtime import (
                    runtime_writers_are_inactive,
                )

                writer_inactive = runtime_writers_are_inactive
            evidence_epoch = AuditEvidenceEpoch.capture(
                required_databases,
                snapshot_root=snapshot_root,
                formal_before=MappingProxyType(before),
                writer_inactive=writer_inactive,
            )
        return cls(
            mode=mode,
            root=root,
            formal_targets=resolved,
            formal_directory_targets=resolved_directories,
            _before=MappingProxyType(before),
            _directory_before=MappingProxyType(directory_before),
            os_write_guard=guard,
            evidence_epoch=evidence_epoch,
        )

    @property
    def database_dir(self) -> Path:
        return self._owned_path("MNEMOS_DATABASE_DIR")

    @property
    def wiki_dir(self) -> Path:
        return self._owned_path("MNEMOS_WIKI_DIR")

    @property
    def runtime_config(self) -> AuditRuntimeConfig:
        if self.mode != "isolated":
            raise RuntimeError("production read-only audit has no writable runtime config")
        database_dir = self.database_dir
        return AuditRuntimeConfig(
            mnemos_dir=database_dir,
            database_dir=database_dir,
            data_dir=database_dir,
            wiki_dir=self.wiki_dir,
        )

    def _owned_path(self, key: str) -> Path:
        if self.mode != "isolated" or self._hermetic is None:
            raise RuntimeError(f"{key} is writable only in an isolated audit")
        return _resolved(self._hermetic.environment[key])

    def create_projection_lifecycle(
        self,
        vault_dir: Path | None = None,
    ) -> DerivedProjectionLifecycle:
        """Create a fully explicit isolated Wiki mutation/publisher chain."""

        if self.mode != "isolated" or self._hermetic is None:
            raise RuntimeError("production read-only audit cannot create projection writers")
        database_dir = self.database_dir
        wiki_dir = _resolved(vault_dir or self.wiki_dir)
        if self.root is None:
            raise RuntimeError("isolated audit has no owned root")
        try:
            wiki_dir.relative_to(self.root)
        except (TypeError, ValueError) as exc:
            raise ValueError("audit Wiki path escapes isolated root") from exc
        config = AuditRuntimeConfig(
            mnemos_dir=database_dir,
            database_dir=database_dir,
            data_dir=database_dir,
            wiki_dir=wiki_dir,
        )
        ledger = WikiProjectionLedger(database_dir / "wiki_projection.db")
        event_bus = EventBus(
            root_dir=database_dir / "events",
            config=config,
            projection_db_path=ledger.db_path,
            run_startup_maintenance=False,
            recover_pending=False,
            enqueue_published_events=False,
        )
        self._event_buses.append(event_bus)
        return DerivedProjectionLifecycle(
            wiki_dir,
            ledger=ledger,
            event_bus=event_bus,
        )

    def open_sqlite_readonly(self, path: Path) -> sqlite3.Connection:
        """Open a declared production target with SQLite's mode=ro contract."""

        if self.mode not in {"production_readonly", "sandbox_readonly"}:
            raise RuntimeError("SQLite reader requires a read-only audit mode")
        target = _resolved(path)
        if target not in self.formal_targets:
            raise ValueError("SQLite read target is absent from the audit inventory")
        if not target.is_file():
            raise FileNotFoundError(target)
        evidence_epoch = self.evidence_epoch
        snapshot = evidence_epoch.snapshot_for(target) if evidence_epoch else None
        if snapshot is not None:
            assert evidence_epoch is not None
            receipt = next(
                (
                    item
                    for item in evidence_epoch.database_snapshots
                    if item.get("source") == str(target)
                ),
                None,
            )
            snapshot_stat = snapshot.lstat()
            if (
                receipt is None
                or snapshot.is_symlink()
                or not stat_module.S_ISREG(snapshot_stat.st_mode)
                or receipt.get("device") != snapshot_stat.st_dev
                or receipt.get("inode") != snapshot_stat.st_ino
                or receipt.get("size") != snapshot_stat.st_size
            ):
                raise RuntimeError("evidence epoch snapshot identity changed before read")
            if receipt.get("sha256") != _sha256_file(snapshot):
                raise RuntimeError("evidence epoch snapshot hash changed before read")
            connection = sqlite3.connect(
                f"{snapshot.resolve(strict=True).as_uri()}?mode=ro&immutable=1",
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            return connection
        connection = sqlite3.connect(
            f"file:{target.resolve(strict=True)}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def target_inventory(self) -> list[dict[str, object]]:
        exact: list[dict[str, object]] = [
            {
                "path": str(path),
                "classification": (
                    "initialized" if bool(signature.get("exists")) else "uninitialized"
                ),
                "signature_kind": "content",
                "signature": signature,
            }
            for path in self.formal_targets
            for signature in (dict(self._before[str(path)]),)
        ]
        directories: list[dict[str, object]] = [
            {
                "path": str(path),
                "classification": (
                    "initialized" if bool(signature.get("exists")) else "uninitialized"
                ),
                "signature_kind": "directory_structure",
                "signature": signature,
            }
            for path in self.formal_directory_targets
            for signature in (dict(self._directory_before[str(path)]),)
        ]
        return exact + directories

    def formal_state_diff(self) -> list[str]:
        if self._cached_diff is not None:
            return list(self._cached_diff)
        return self._calculate_diff()

    def _calculate_diff(
        self,
        *,
        exact_diff: set[str] | None = None,
    ) -> list[str]:
        if exact_diff is None:
            after = {str(path): path_signature(path) for path in self.formal_targets}
            exact_diff = {
                path
                for path in set(self._before) | set(after)
                if self._before.get(path) != after.get(path)
            }
        directory_after = {
            str(path): directory_structure_signature(path) for path in self.formal_directory_targets
        }
        directory_diff = {
            path
            for path in set(self._directory_before) | set(directory_after)
            if self._directory_before.get(path) != directory_after.get(path)
        }
        return sorted(exact_diff | directory_diff)

    def report(self) -> dict[str, object]:
        diff = self.formal_state_diff()
        report: dict[str, object] = {
            "schema_version": AUDIT_RUN_SCHEMA_VERSION,
            "mode": self.mode,
            "target_inventory": self.target_inventory(),
            "outside_write_count": len(diff),
            "formal_state_diff": diff,
            "os_write_guard": self.os_write_guard,
        }
        if self._hermetic is not None:
            report.update(
                {
                    "sandbox_root": str(self._hermetic.root),
                    "environment_hash": self._hermetic.environment_hash,
                    "manifest_path": str(self._hermetic.manifest_path),
                }
            )
        elif self.mode == "sandbox_readonly" and self.root is not None:
            report["sandbox_root"] = str(self.root)
        if self.evidence_epoch is not None:
            report["evidence_epoch"] = self.evidence_epoch.report()
        return report

    def close(self) -> None:
        if self._closed:
            return
        close_errors: list[str] = []
        for event_bus in reversed(self._event_buses):
            try:
                event_bus.close()
            except Exception as exc:  # noqa: BLE001 - diff must still run
                logger.warning("audit EventBus close failed", exc_info=True)
                close_errors.append(f"EventBus close failed: {exc}")
        exact_diff: set[str] | None = None
        if self._hermetic is not None:
            try:
                exact_diff = set(self._hermetic.finalize())
            except Exception as exc:  # noqa: BLE001 - diff must still run
                logger.warning("audit hermetic finalize failed", exc_info=True)
                close_errors.append(f"hermetic finalize failed: {exc}")
        diff = self._calculate_diff(exact_diff=exact_diff)
        self._cached_diff = tuple(diff)
        self._closed = True
        if diff or close_errors:
            details = close_errors + (["formal state diff: " + ", ".join(diff)] if diff else [])
            raise RuntimeError("audit execution boundary failed closed: " + "; ".join(details))

    def __enter__(self) -> "AuditExecutionEnvironment":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
