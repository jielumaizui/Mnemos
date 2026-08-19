"""Tests for the bounded, Raw-only 12-source reconciliation entry point."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
import zlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import Mock

import pytest

from core.agent_kit import native_raw_challenger
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn
from core.sync_framework.native_artifact_inventory import (
    NativeArtifactInventoryError,
    build_native_artifact_inventory,
    snapshot_native_sources,
)
from core.sync_framework.native_artifact_models import (
    SNAPSHOT_PARSE_TERMINAL_ERROR_CODES,
)
from core.sync_framework.native_raw_contract_ledger import (
    NativeRawContractLedger,
)
from core.sync_framework.raw_event_identity import (
    RawEventIdentitySchemaMigrationRequired,
)
from core.sync_framework.raw_event_store import RawEventStore
from core.sync_framework import raw_current_projection_reconciliation
from core.sync_framework.sync_engine import (
    CanonicalRawCommitError,
    SyncEngine,
)
from daemon.agent_sync_cursor import AgentSyncCursorStore
from scripts import agent_source_raw_migration_certification as migration_certification
from scripts import agent_source_raw_recovery_support as recovery_support
from scripts import reconcile_agent_source_raw_capture as reconciler

_REAL_PHASE1_GOVERNANCE_BINDING = (
    reconciler._phase1_governance_generation_binding  # noqa: SLF001
)


@contextmanager
def _sqlite_transaction(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class _Config:
    def __init__(self, root: Path):
        self.database_dir = root
        self.data_dir = root

    def get(self, _key, default=None):
        return default


@pytest.fixture(autouse=True)
def _bind_synthetic_phase1_governance_generation(
    monkeypatch: pytest.MonkeyPatch,
):
    """Every hermetic recovery fixture owns an explicit reviewed generation."""
    monkeypatch.setattr(
        reconciler,
        "_phase1_governance_generation_binding",
        lambda: {
            "schema_version": "mnemos.phase1_recovery_governance_binding.v1",
            "ok": True,
            "record_id": "phase1-hermetic-reviewed-generation",
            "record_hash": "sha256:" + ("1" * 64),
            "execution_evidence_hash": "2" * 64,
            "execution_evidence_file_sha256": "3" * 64,
            "candidate_snapshot": {
                "path_count": 1,
                "sha256": "4" * 64,
            },
            "current_candidate_snapshot": {
                "path_count": 1,
                "sha256": "4" * 64,
            },
            "post_deep_review_contract_hash": "sha256:" + ("5" * 64),
            "sequence_predecessor": "phase1-hermetic-predecessor",
            "governance_data_sha256": "6" * 64,
            "errors": [],
        },
    )


class _Source(AgentSource):
    name = "codex"
    model_tag = "synthetic-codex"

    def __init__(self, path: Path, name: str = "codex"):
        self.path = path
        self.name = name
        self.model_tag = f"synthetic-{name}"

    def discover_sessions(self):
        return [SessionInfo(session_id="session", source_path=self.path)]

    def parse_turns(self, _path: Path):
        return [
            Turn(
                turn_number=0,
                user_content="synthetic-user",
                assistant_content="synthetic-assistant",
                native_event_id="synthetic-native-0",
            )
        ]

    def completeness_capabilities(self):
        return {
            "visible_text": True,
            "tool_calls": True,
            "tool_results": True,
            "reasoning": True,
            "attachments": True,
            "raw_files": True,
            "source_fidelity": "full",
        }


@pytest.mark.parametrize(
    ("operation", "code"),
    [
        (
            lambda target, backup_dir: recovery_support._file_scope(target),
            "recovery_state_path_unavailable",
        ),
        (
            lambda target, backup_dir: recovery_support._backup_sqlite(
                target,
                backup_dir,
                "pre-raw",
            ),
            "sqlite_backup_source_unavailable",
        ),
        (
            lambda target, backup_dir: recovery_support._backup_coverage(
                target,
                backup_dir,
            ),
            "coverage_backup_source_unavailable",
        ),
    ],
)
def test_recovery_state_inspection_never_folds_unavailable_into_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation,
    code: str,
) -> None:
    target = tmp_path / "state"
    original_stat = Path.stat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(
        recovery_support.AgentSourceRawReconciliationError,
        match=code,
    ):
        operation(target, tmp_path / "backups")


@pytest.mark.parametrize("kind", ("sqlite", "coverage"))
def test_failed_backup_creation_removes_its_unbound_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    source = tmp_path / ("state.db" if kind == "sqlite" else "coverage.json")
    if kind == "sqlite":
        with sqlite3.connect(source) as connection:
            connection.execute("CREATE TABLE sentinel (value TEXT)")
    else:
        source.write_text('{"sentinel":true}\n', encoding="utf-8")
    backup_dir = tmp_path / "backups"
    original_create = recovery_support._create_private_target

    def create_then_abort(path: Path) -> None:
        original_create(path)
        raise OSError("sentinel backup interruption")

    monkeypatch.setattr(
        recovery_support,
        "_create_private_target",
        create_then_abort,
    )

    with pytest.raises(
        recovery_support.AgentSourceRawReconciliationError,
        match=("sqlite_backup_failed" if kind == "sqlite" else "coverage_backup_failed"),
    ):
        if kind == "sqlite":
            recovery_support._backup_sqlite(source, backup_dir, "pre-state")
        else:
            recovery_support._backup_coverage(source, backup_dir)

    assert list(backup_dir.iterdir()) == []


def test_later_backup_failure_removes_all_earlier_unbound_backups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _Config(tmp_path)
    raw_path = tmp_path / "raw_events.db"
    raw_path.write_bytes(b"raw-sentinel")
    backup_dir = tmp_path / "backups"
    first_backup = backup_dir / "first.sqlite"
    calls = 0

    monkeypatch.setattr(
        reconciler,
        "_validate_active_sources",
        lambda _sources, **_kwargs: ([SimpleNamespace(name="codex")], ["codex"]),
    )
    monkeypatch.setattr(
        reconciler,
        "_audit_native_to_raw_isolated",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(reconciler, "_recovery_plan", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        reconciler,
        "plan_current_projection_reconciliation",
        lambda _path: {"ok": True},
    )

    def backup_then_fail(_path: Path, directory: Path, _label: str):
        nonlocal calls
        calls += 1
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if calls == 1:
            first_backup.write_bytes(b"complete-first-backup")
            return first_backup, {
                "present": True,
                "filename": first_backup.name,
                "sha256": "sentinel",
            }
        raise reconciler.AgentSourceRawReconciliationError("second_backup_failed")

    monkeypatch.setattr(reconciler, "_backup_sqlite", backup_then_fail)
    monkeypatch.setattr(
        reconciler,
        "_backup_coverage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("coverage backup must not run after cursor backup fails")
        ),
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="second_backup_failed",
    ):
        reconciler._reconcile_active_source_raw_capture_unlocked(  # noqa: SLF001
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[SimpleNamespace(name="codex")],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            session_identity_reconciliation={"ok": True, "receipts": []},
            reviewed_plan_hash="sha256:" + ("f" * 64),
        )

    assert calls == 2
    assert list(backup_dir.iterdir()) == []


def test_same_plan_wal_inspection_never_folds_unavailable_into_quiescent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "raw_events.db"
    wal_path = Path(f"{database}-wal")
    original_stat = Path.stat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == wal_path:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(
        recovery_support.AgentSourceRawReconciliationError,
        match="same_plan_live_wal_unreadable",
    ):
        migration_certification._require_quiescent_sqlite_main_file(database)


def test_raw_generation_write_guard_requires_complete_database_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_iterdir = Path.iterdir

    def denied(path: Path):
        if path == tmp_path:
            raise PermissionError("sentinel")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied)

    with pytest.raises(
        recovery_support.AgentSourceRawReconciliationError,
        match="database_scope_inspection_unavailable",
    ):
        reconciler._database_file_state(tmp_path, set())


class _EmptySource(AgentSource):
    name = "aider"
    model_tag = "synthetic-aider"

    def __init__(self, root: Path):
        self.root = root

    @property
    def data_dir(self):
        return self.root

    def discover_sessions(self):
        return []

    def parse_turns(self, _path: Path):
        return []


class _IdentityUpgradeSource(_Source):
    name = "openclaw"
    model_tag = "synthetic-openclaw"

    def __init__(self, path: Path):
        super().__init__(path, name="openclaw")

    def discover_sessions(self):
        return [
            SessionInfo(
                session_id="current-session",
                source_path=self.path,
                canonical_session_id="current-session",
                session_aliases=["legacy-session"],
                metadata={
                    "identity_contract_version": "synthetic-artifact-v2",
                    "identity_reconciliation_required": True,
                    "legacy_canonical_session_ids": ["legacy-session"],
                    "source_artifact_id": "artifact-current",
                },
            )
        ]


def _reviewed_plan(
    *,
    config: _Config,
    raw_path: Path,
    backup_dir: Path,
    source: _Source,
    reset_derived_state: bool = True,
    writers_inactive: bool = True,
):
    return reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=False,
        cycles=2,
        reset_derived_state=reset_derived_state,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: writers_inactive,
    )


def test_recovery_roster_requires_all_twelve_active_sources(tmp_path: Path):
    path = tmp_path / "native.jsonl"
    path.write_text("synthetic-safe", encoding="utf-8")
    manifest = reconciler.get_agent_source_support_manifest()
    sources = [_Source(path, name) for name in manifest.active_source_names]

    selected, names = reconciler._validate_active_sources(  # noqa: SLF001
        sources,
        require_all_active_sources=True,
    )

    assert len(selected) == len(names) == 12
    assert "aider" in names
    assert "codex" in names
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="active_source_roster_incomplete",
    ):
        reconciler._validate_active_sources(  # noqa: SLF001
            [source for source in sources if source.name != "aider"],
            require_all_active_sources=True,
        )


def test_cli_loader_instantiates_all_twelve_manifest_active_parsers(
    monkeypatch: pytest.MonkeyPatch,
):
    def source_class(source_name: str):
        return type(
            f"Synthetic{source_name.title()}Source",
            (),
            {"name": source_name},
        )

    monkeypatch.setattr(
        reconciler.SourceRegistry,
        "get_builtin_source_class",
        source_class,
    )

    sources = reconciler.load_manifest_active_sources()

    assert len(sources) == 12
    assert {source.name for source in sources} == set(
        reconciler.get_agent_source_support_manifest().active_source_names
    )


def test_cli_requires_explicit_native_history_read_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setattr(reconciler, "Config", lambda **_kwargs: _Config(tmp_path))
    monkeypatch.setattr(
        reconciler,
        "load_manifest_active_sources",
        lambda: (_ for _ in ()).throw(AssertionError("history discovery must not run")),
    )

    exit_code = reconciler.main(["--backup-dir", str(tmp_path / "backups"), "--json"])

    assert exit_code == 1
    assert (
        json.loads(capsys.readouterr().out)["error_code"]
        == "native_history_read_confirmation_required"
    )


def test_cli_can_archive_full_plan_while_printing_a_bounded_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_hash = "sha256:" + ("a" * 64)
    full_result = {
        "schema_version": reconciler.SCHEMA_VERSION,
        "mode": "dry_run",
        "ok": True,
        "apply_eligible": True,
        "current_state_ok": False,
        "writer_lock_state": "writers_inactive",
        "plan_hash": plan_hash,
        "active_sources": ["codex"],
        "native_artifact_inventory": {
            "inventory_hash": "sha256:" + ("b" * 64),
            "source_count": 1,
            "artifact_count": 1,
            "entries": [
                {
                    "artifact_identity_hash": "sha256:" + ("c" * 64),
                    "content_hash": "sha256:" + ("d" * 64),
                }
            ],
        },
        "session_identity_reconciliation": {
            "ok": True,
            "ambiguous_count": 0,
            "unresolved_count": 0,
        },
        "current_projection_reconciliation": {
            "ok": True,
            "invalid_count": 13,
            "restore_revision_count": 11,
            "append_revision_count": 2,
            "blocked_count": 0,
        },
    }
    monkeypatch.setattr(reconciler, "Config", lambda **_kwargs: _Config(tmp_path))
    monkeypatch.setattr(
        reconciler,
        "load_manifest_active_sources",
        lambda: [SimpleNamespace(name="codex")],
    )
    monkeypatch.setattr(
        reconciler,
        "reconcile_active_source_raw_capture",
        lambda **_kwargs: dict(full_result),
    )
    archive_path = tmp_path / "plans" / "full-plan.json"

    exit_code = reconciler.main(
        [
            "--confirm-read-native-history",
            "--summary-json",
            "--output-json",
            str(archive_path),
        ]
    )
    summary = json.loads(capsys.readouterr().out)
    archived = json.loads(archive_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert summary["schema_version"] == "mnemos.agent_source_raw_cli_summary.v2"
    assert summary["plan_hash"] == plan_hash
    assert summary["native_artifact_inventory"]["artifact_count"] == 1
    assert "entries" not in summary["native_artifact_inventory"]
    assert summary["current_projection_reconciliation"] == {
        "append_revision_count": 2,
        "blocked_count": 0,
        "invalid_count": 13,
        "ok": True,
        "restore_revision_count": 11,
    }
    assert summary["full_result_sha256"] == reconciler._canonical_hash(full_result)
    assert summary["output_json"] == {
        "status": "written",
        "path": str(archive_path.resolve()),
        "sha256": f"sha256:{reconciler._file_sha256(archive_path)}",
    }
    assert archived == full_result


def test_plan_archive_never_chmods_an_existing_parent_directory(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "shared-output"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)
    target = parent / "plan.json"

    with pytest.raises(
        recovery_support.AgentSourceRawReconciliationError,
        match="new_receipt_parent_unsafe",
    ):
        recovery_support._write_new_receipt(  # noqa: SLF001
            target,
            {"plan_hash": "sha256:" + ("a" * 64)},
        )

    assert parent.stat().st_mode & 0o777 == 0o755
    assert target.exists() is False
    assert not target.exists()


def test_cli_help_explains_private_output_json_parent_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        reconciler.main(["--help"])

    assert exit_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "owner-only 0700 directory" in help_text
    assert "never chmodded" in help_text


def test_cli_archive_collision_preserves_plan_summary_and_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_hash = "sha256:" + ("e" * 64)
    full_result = {
        "schema_version": reconciler.SCHEMA_VERSION,
        "mode": "dry_run",
        "ok": True,
        "apply_eligible": True,
        "plan_hash": plan_hash,
    }
    monkeypatch.setattr(reconciler, "Config", lambda **_kwargs: _Config(tmp_path))
    monkeypatch.setattr(
        reconciler,
        "load_manifest_active_sources",
        lambda: [SimpleNamespace(name="codex")],
    )
    monkeypatch.setattr(
        reconciler,
        "reconcile_active_source_raw_capture",
        lambda **_kwargs: dict(full_result),
    )
    archive_path = tmp_path / "plans" / "full-plan.json"
    archive_path.parent.mkdir(mode=0o700)
    os.chmod(archive_path.parent, 0o700)
    archive_path.write_text("sentinel-existing-plan\n", encoding="utf-8")

    exit_code = reconciler.main(
        [
            "--confirm-read-native-history",
            "--summary-json",
            "--output-json",
            str(archive_path),
        ]
    )
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert summary["plan_hash"] == plan_hash
    assert summary["full_result_sha256"] == reconciler._canonical_hash(full_result)
    assert summary["output_json"]["status"] == "failed"
    assert summary["output_json"]["error_code"] == "output_json_write_failed"
    assert archive_path.read_text(encoding="utf-8") == "sentinel-existing-plan\n"


def test_explicit_current_codex_cutoff_is_local_hashed_and_reenterable():
    active_id = "123e4567-e89b-12d3-a456-426614174000"
    closed_id = "223e4567-e89b-12d3-a456-426614174000"

    class _Codex:
        name = "codex"
        model_tag = "codex"

        @staticmethod
        def current_active_session_id():
            return active_id

        @staticmethod
        def discover_sessions():
            return [
                SimpleNamespace(
                    session_id=active_id,
                    canonical_session_id=active_id,
                ),
                SimpleNamespace(
                    session_id=closed_id,
                    canonical_session_id=closed_id,
                ),
            ]

    wrapped = reconciler._with_explicit_current_codex_cutoff([_Codex()])[0]  # noqa: SLF001

    assert [item.canonical_session_id for item in wrapped.discover_sessions()] == [closed_id]
    evidence = wrapped.deferred_active_session_evidence()
    assert evidence["deferred_count"] == 1
    assert evidence["scope"] == "explicit_offline_reconciliation_only"
    assert active_id not in str(evidence)


def test_explicit_current_codex_cutoff_refuses_unmatched_environment_identity():
    class _Codex:
        name = "codex"
        model_tag = "codex"

        @staticmethod
        def current_active_session_id():
            return "123e4567-e89b-12d3-a456-426614174000"

        @staticmethod
        def discover_sessions():
            return [
                SimpleNamespace(
                    session_id="223e4567-e89b-12d3-a456-426614174000",
                    canonical_session_id=("223e4567-e89b-12d3-a456-426614174000"),
                )
            ]

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="current_codex_cutoff_session_not_exact",
    ):
        reconciler._with_explicit_current_codex_cutoff([_Codex()])  # noqa: SLF001


def test_public_raw_apply_supports_same_plan_verified_noop(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()

    source = _Source(source_path)
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=source,
    )
    snapshot = preview["native_artifact_snapshot"]
    assert snapshot["schema_version"] == "mnemos.native_source_artifact_snapshot.v4"
    assert snapshot["parser_private_temp_contract"] == (
        "generic-temp-bound-to-private-parse-spool-sqlite-memory-v2"
    )
    assert snapshot["native_sqlite_temp_store"] == (
        "connection-local-memory-with-session-rss-budget-v1"
    )
    assert snapshot["challenger_identity_materialization"] == (
        "per-session-exit-reclaimed-content-free-pipe-v1"
    )
    assert snapshot["parser_isolation"] == ("plan-bound-private-artifact-snapshot-v4")
    assert snapshot["sqlite_snapshot_journal_mode"] == "delete"
    assert snapshot["sqlite_snapshot_sidecar_count"] == 0
    assert "stabilization_attempts" not in snapshot
    assert "stale_snapshot_dirs_cleaned" not in snapshot
    assert snapshot["parse_batching"] == "session-bounded"
    assert snapshot["post_live_inventory_check"] == "required"
    assert snapshot["artifact_count"] == 1
    assert snapshot["preparse_logical_bytes"] == len(b"synthetic-safe")
    assert set(snapshot["parser_source_hashes"]) == {"codex"}

    first = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=tmp_path / "backups",
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )
    second = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=tmp_path / "backups",
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )

    assert first["first_apply"]["comparator_ok"] is True
    assert first["restore_drill_ok"] is True
    assert first["second_apply_changed"] is False
    assert second["mode"] == "same_plan_second_apply"
    assert second["physical_delta"] == 0
    assert second["semantic_delta"] == 0
    assert second["required_gap"] == 0
    assert second["physical_pre_signature"] == second["physical_post_signature"]


def test_public_raw_apply_rebuilds_legacy_coverage_after_backing_it_up(
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    coverage_path = reconciler.agent_source_coverage.coverage_state_path(config.database_dir)
    legacy_bytes = (
        json.dumps(
            {
                "schema_version": "mnemos.agent_source_coverage.v1",
                "support_manifest_hash": "legacy",
                "sources": {},
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    coverage_path.write_bytes(legacy_bytes)
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )

    applied = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )

    assert applied["ok"] is True
    assert applied["coverage_state_reset"] is True
    assert applied["backups"]["coverage"]["present"] is True
    backup_name = applied["backups"]["coverage"]["filename"]
    assert (backup_dir / backup_name).read_bytes() == legacy_bytes
    rebuilt = reconciler.agent_source_coverage.load_source_coverage_state(coverage_path)
    assert (
        rebuilt["schema_version"] == reconciler.agent_source_coverage.SOURCE_COVERAGE_SCHEMA_VERSION
    )


def test_public_raw_apply_rolls_back_when_legacy_coverage_reset_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    coverage_path = reconciler.agent_source_coverage.coverage_state_path(config.database_dir)
    legacy_bytes = (
        json.dumps(
            {
                "schema_version": "mnemos.agent_source_coverage.v1",
                "support_manifest_hash": "legacy",
                "sources": {},
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    coverage_path.write_bytes(legacy_bytes)
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    real_unlink = reconciler._unlink_targets_durably  # noqa: SLF001

    def fail_coverage_reset(
        targets,
        *,
        error_code: str,
    ) -> None:
        resolved_targets = tuple(Path(target).resolve() for target in targets)
        if (
            resolved_targets == (coverage_path.resolve(),)
            and error_code == "coverage_state_reset_failed"
        ):
            raise reconciler.AgentSourceRawReconciliationError(error_code)
        real_unlink(targets, error_code=error_code)

    monkeypatch.setattr(
        reconciler,
        "_unlink_targets_durably",
        fail_coverage_reset,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="coverage_state_reset_failed",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    assert coverage_path.read_bytes() == legacy_bytes
    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        preview["plan_hash"],
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "recovered_rollback"
    assert receipt["inner_error_code"] == "coverage_state_reset_failed"
    assert receipt["rollback_ok"] is True


def test_public_raw_apply_accepts_a_recovered_retryable_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    real_run_service = reconciler.raw_sync.run_service

    def inject_first_generation_retryable_error(log_service_error, **kwargs):
        result = dict(real_run_service(log_service_error, **kwargs))
        if reconciler._ACTIVE_RAW_GENERATION_NUMBER == 1:  # noqa: SLF001
            log_service_error(
                "raw_sync:codex",
                reconciler.AgentSourceRawReconciliationError("transient_capture_unavailable"),
            )
            result["errors"] = int(result.get("errors") or 0) + 1
        return result

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        inject_first_generation_retryable_error,
    )

    applied = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )

    assert applied["ok"] is True
    assert applied["retryable_error_count"] == 1
    assert applied["recovered_retryable_error_count"] == 1
    assert applied["unrecovered_retryable_error_count"] == 0
    assert [cycle["errors"] for cycle in applied["cycles"]] == [1, 0]
    assert applied["required_gap"] == 0


def test_retryable_source_failure_can_recover_in_later_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    real_run_service = reconciler.raw_sync.run_service

    def inject_first_generation_retryable_error(log_service_error, **kwargs):
        result = dict(real_run_service(log_service_error, **kwargs))
        if reconciler._ACTIVE_RAW_GENERATION_NUMBER == 1:  # noqa: SLF001
            log_service_error(
                "raw_sync:codex",
                reconciler.AgentSourceRawReconciliationError("transient_capture_unavailable"),
            )
            result["errors"] = int(result.get("errors") or 0) + 1
        return result

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        inject_first_generation_retryable_error,
    )

    result = reconciler._execute_unresolved_active_source_raw_capture_for_test(  # noqa: SLF001
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )

    assert result["ok"] is True
    assert result["retryable_error_codes"] == ["transient_capture_unavailable"]
    assert result["retryable_error_count"] == 1
    assert result["recovered_retryable_error_count"] == 1
    assert result["unrecovered_retryable_error_count"] == 0
    assert result["source_capture"]["codex"]["ok"] is True


def test_retryable_source_failure_still_fails_when_final_gap_remains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    real_run_service = reconciler.raw_sync.run_service

    def inject_unattributed_retryable_error(log_service_error, **kwargs):
        result = dict(real_run_service(log_service_error, **kwargs))
        if reconciler._ACTIVE_RAW_GENERATION_NUMBER == 2:  # noqa: SLF001
            log_service_error(
                "raw_sync:unattributed",
                reconciler.AgentSourceRawReconciliationError("transient_capture_unavailable"),
            )
            result["errors"] = int(result.get("errors") or 0) + 1
        return result

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        inject_unattributed_retryable_error,
    )

    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_reconciliation_incomplete",
    ):
        reconciler._execute_unresolved_active_source_raw_capture_for_test(  # noqa: SLF001
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    receipt = json.loads(
        next(backup_dir.glob("agent-source-raw-reconciliation-*.json")).read_text(encoding="utf-8")
    )
    assert receipt["status"] == "failed"
    assert receipt["rollback_ok"] is True
    assert receipt["retryable_error_count"] == 1
    assert receipt["recovered_retryable_error_count"] == 0
    assert receipt["unrecovered_retryable_error_count"] == 1
    assert receipt["after_challenger"]["ok"] is True
    assert receipt["source_capture"]["codex"]["ok"] is True
    assert receipt["failure_reasons"] == ["retryable_errors_unrecovered"]


def test_public_raw_apply_rejects_deterministic_native_budget_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    real_run_service = reconciler.raw_sync.run_service

    def fail_first_generation(log_service_error, **kwargs):
        result = dict(real_run_service(log_service_error, **kwargs))
        if reconciler._ACTIVE_RAW_GENERATION_NUMBER == 1:  # noqa: SLF001
            log_service_error(
                "raw_sync:codex",
                NativeArtifactInventoryError("native_freeze_worker_budget_exceeded"),
            )
            result["errors"] = int(result.get("errors") or 0) + 1
        return result

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        fail_first_generation,
    )

    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_reconciliation_nonretryable_source_failure",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    receipt = json.loads(
        reconciler._migration_receipt_path(  # noqa: SLF001
            backup_dir,
            preview["plan_hash"],
        ).read_text(encoding="utf-8")
    )
    assert receipt["status"] == "recovered_rollback"
    inner = json.loads((backup_dir / receipt["inner_receipt_filename"]).read_text(encoding="utf-8"))
    assert inner["status"] == "rolled_back_by_migration_certification"
    assert inner["terminal_failure_codes"] == ["native_freeze_worker_budget_exceeded"]


def test_public_apply_rejects_unscoped_retry_error_and_can_retry_same_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    real_run_service = reconciler.raw_sync.run_service
    calls = 0

    def inject_unscoped_error(log_service_error, **kwargs):
        nonlocal calls
        calls += 1
        result = dict(real_run_service(log_service_error, **kwargs))
        if calls == 1:
            log_service_error(
                "raw_sync",
                NativeArtifactInventoryError("native_freeze_worker_budget_exceeded"),
            )
            result["errors"] = int(result.get("errors") or 0) + 1
        return result

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        inject_unscoped_error,
    )
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_reconciliation_nonretryable_source_failure",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    outer_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        preview["plan_hash"],
    )
    assert json.loads(outer_path.read_text(encoding="utf-8"))["status"] == ("recovered_rollback")
    failed_receipt_bytes = outer_path.read_bytes()
    failed_receipt = json.loads(failed_receipt_bytes)
    assert len(failed_receipt["inner_prepared_receipt_sha256"]) == 64
    assert len(failed_receipt["inner_receipt_sha256"]) == 64

    tampered_receipt = dict(failed_receipt)
    tampered_receipt["schema_version"] = "tampered"
    outer_path.write_text(
        json.dumps(tampered_receipt, sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_binding_mismatch",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )
    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    outer_path.write_bytes(failed_receipt_bytes)

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        real_run_service,
    )
    resumed = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )
    assert resumed["ok"] is True
    assert resumed["required_gap"] == 0
    history = list(
        backup_dir.glob(
            "agent-source-raw-migration-history."
            f"{preview['plan_hash'].removeprefix('sha256:')}.*.json"
        )
    )
    assert len(history) == 1
    assert history[0].read_bytes() == failed_receipt_bytes
    extra_payload = {
        **failed_receipt,
        "unexpected_history_generation": True,
    }
    extra_bytes = reconciler._receipt_bytes(extra_payload)  # noqa: SLF001
    extra_path = backup_dir / (
        "agent-source-raw-migration-history."
        f"{preview['plan_hash'].removeprefix('sha256:')}."
        f"{hashlib.sha256(extra_bytes).hexdigest()}.json"
    )
    extra_path.write_bytes(extra_bytes)
    extra_path.chmod(0o600)
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_history_invalid",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )
    extra_path.unlink()
    history[0].unlink()
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_history_invalid",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )


@pytest.mark.parametrize("failure_count", (1, 2, 3))
def test_same_plan_preserves_every_terminal_failure_across_multiple_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_count: int,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        preview["plan_hash"],
    )
    real_post_gap = reconciler._post_apply_raw_gap  # noqa: SLF001
    monkeypatch.setattr(
        reconciler,
        "_post_apply_raw_gap",
        lambda **_kwargs: {
            "schema_version": "mnemos.agent_source_raw_post_gap.v1",
            "required_gap": 1,
            "ok": False,
        },
    )

    failed_receipt_bytes: list[bytes] = []
    for _attempt in range(failure_count):
        with pytest.raises(
            reconciler.AgentSourceRawReconciliationError,
            match="post_apply_gap_nonzero",
        ):
            reconciler.reconcile_active_source_raw_capture(
                config=config,
                raw_db_path=raw_path,
                backup_dir=backup_dir,
                sources=[source],
                apply=True,
                require_all_active_sources=False,
                runtime_writers_are_inactive=lambda: True,
                expected_plan_hash=preview["plan_hash"],
            )
        failed_receipt_bytes.append(receipt_path.read_bytes())
        assert json.loads(failed_receipt_bytes[-1])["status"] == ("recovered_rollback")

    monkeypatch.setattr(
        reconciler,
        "_post_apply_raw_gap",
        real_post_gap,
    )
    completed = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )
    repeated = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )

    history = sorted(
        backup_dir.glob(
            "agent-source-raw-migration-history."
            f"{preview['plan_hash'].removeprefix('sha256:')}.*.json"
        )
    )
    final_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert completed["ok"] is True
    assert repeated["mode"] == "same_plan_second_apply"
    assert len(history) == failure_count
    assert {path.read_bytes() for path in history} == set(failed_receipt_bytes)
    assert set(final_receipt["prior_terminal_receipts"]) == {path.name for path in history}


def test_same_plan_retry_before_prepared_preserves_original_error_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        preview["plan_hash"],
    )
    real_post_gap = reconciler._post_apply_raw_gap  # noqa: SLF001
    monkeypatch.setattr(
        reconciler,
        "_post_apply_raw_gap",
        lambda **_kwargs: {
            "schema_version": "mnemos.agent_source_raw_post_gap.v1",
            "required_gap": 1,
            "ok": False,
        },
    )
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="post_apply_gap_nonzero",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )
    failed_receipt_bytes = receipt_path.read_bytes()
    monkeypatch.setattr(
        reconciler,
        "_post_apply_raw_gap",
        real_post_gap,
    )
    real_execute = reconciler._execute_unresolved_active_source_raw_capture_for_test

    def fail_before_prepared(**kwargs):
        if kwargs["apply"]:
            raise reconciler.AgentSourceRawReconciliationError("raw_current_projection_plan_drift")
        return real_execute(**kwargs)

    monkeypatch.setattr(
        reconciler,
        "_execute_unresolved_active_source_raw_capture_for_test",
        fail_before_prepared,
    )
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_current_projection_plan_drift",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )

    history = sorted(
        backup_dir.glob(
            "agent-source-raw-migration-history."
            f"{preview['plan_hash'].removeprefix('sha256:')}.*.json"
        )
    )
    assert len(history) == 1
    assert history[0].read_bytes() == failed_receipt_bytes
    assert receipt_path.read_bytes() == failed_receipt_bytes


def test_same_plan_rejects_reordered_terminal_failure_lineage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    real_post_gap = reconciler._post_apply_raw_gap  # noqa: SLF001
    monkeypatch.setattr(
        reconciler,
        "_post_apply_raw_gap",
        lambda **_kwargs: {
            "schema_version": "mnemos.agent_source_raw_post_gap.v1",
            "required_gap": 1,
            "ok": False,
        },
    )
    for _attempt in range(2):
        with pytest.raises(
            reconciler.AgentSourceRawReconciliationError,
            match="post_apply_gap_nonzero",
        ):
            reconciler.reconcile_active_source_raw_capture(
                config=config,
                raw_db_path=raw_path,
                backup_dir=backup_dir,
                sources=[source],
                apply=True,
                require_all_active_sources=False,
                runtime_writers_are_inactive=lambda: True,
                expected_plan_hash=preview["plan_hash"],
            )
    monkeypatch.setattr(
        reconciler,
        "_post_apply_raw_gap",
        real_post_gap,
    )
    reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )

    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        preview["plan_hash"],
    )
    completed = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert len(completed["prior_terminal_receipts"]) == 2
    completed["prior_terminal_receipts"].reverse()
    reconciler._write_receipt(receipt_path, completed)  # noqa: SLF001

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_history_invalid",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )


def test_public_apply_rejects_unattributed_cycle_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    real_run_service = reconciler.raw_sync.run_service
    calls = 0

    def omit_typed_error(log_service_error, **kwargs):
        nonlocal calls
        calls += 1
        result = dict(real_run_service(log_service_error, **kwargs))
        if calls == 1:
            result["errors"] = int(result.get("errors") or 0) + 1
        return result

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        omit_typed_error,
    )
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_reconciliation_incomplete",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        preview["plan_hash"],
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "recovered_rollback"
    inner = json.loads((backup_dir / receipt["inner_receipt_filename"]).read_text(encoding="utf-8"))
    assert inner["failure_reasons"] == ["cycle_error_evidence_mismatch"]


def test_same_plan_retry_recovers_after_exit_between_history_archive_and_intent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    real_post_gap = reconciler._post_apply_raw_gap  # noqa: SLF001
    monkeypatch.setattr(
        reconciler,
        "_post_apply_raw_gap",
        lambda **_kwargs: {
            "schema_version": "mnemos.agent_source_raw_post_gap.v1",
            "required_gap": 1,
            "ok": False,
        },
    )
    for _attempt in range(2):
        with pytest.raises(
            reconciler.AgentSourceRawReconciliationError,
            match="post_apply_gap_nonzero",
        ):
            reconciler.reconcile_active_source_raw_capture(
                config=config,
                raw_db_path=raw_path,
                backup_dir=backup_dir,
                sources=[source],
                apply=True,
                require_all_active_sources=False,
                runtime_writers_are_inactive=lambda: True,
                expected_plan_hash=preview["plan_hash"],
            )
    monkeypatch.setattr(
        reconciler,
        "_post_apply_raw_gap",
        real_post_gap,
    )

    def exit_after_history_archive():
        real_archive = reconciler._archive_terminal_migration_receipt  # noqa: SLF001

        def archive_then_exit(**kwargs):
            real_archive(**kwargs)
            os._exit(79)

        reconciler._archive_terminal_migration_receipt = archive_then_exit  # noqa: SLF001
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )

    process = multiprocessing.get_context("fork").Process(target=exit_after_history_archive)
    process.start()
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise AssertionError("history archive fault-injection child did not terminate")
    assert process.exitcode == 79

    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        preview["plan_hash"],
    )
    assert json.loads(receipt_path.read_text())["status"] == ("recovered_rollback")
    history = list(
        backup_dir.glob(
            "agent-source-raw-migration-history."
            f"{preview['plan_hash'].removeprefix('sha256:')}.*.json"
        )
    )
    assert len(history) == 2

    resumed = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )
    repeated = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )

    assert resumed["ok"] is True
    assert repeated["mode"] == "same_plan_second_apply"
    completed = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert completed["prior_terminal_receipts"] == [
        path.name
        for path in sorted(
            history,
            key=lambda path: json.loads(path.read_text(encoding="utf-8"))[
                "prior_terminal_receipts"
            ],
        )
    ]


def test_same_plan_preserves_failure_lineage_through_prepared_process_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        preview["plan_hash"],
    )
    real_post_gap = reconciler._post_apply_raw_gap  # noqa: SLF001
    monkeypatch.setattr(
        reconciler,
        "_post_apply_raw_gap",
        lambda **_kwargs: {
            "schema_version": "mnemos.agent_source_raw_post_gap.v1",
            "required_gap": 1,
            "ok": False,
        },
    )
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="post_apply_gap_nonzero",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )
    monkeypatch.setattr(
        reconciler,
        "_post_apply_raw_gap",
        real_post_gap,
    )

    def exit_with_prepared_receipt() -> None:
        reconciler._post_apply_raw_gap = lambda **_kwargs: os._exit(80)  # noqa: SLF001
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )

    process = multiprocessing.get_context("fork").Process(target=exit_with_prepared_receipt)
    process.start()
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise AssertionError("prepared lineage fault-injection child did not terminate")
    assert process.exitcode == 80
    prepared = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert prepared["status"] == "prepared"
    assert len(prepared["prior_terminal_receipts"]) == 1

    resumed = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )
    repeated = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )

    completed = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert resumed["ok"] is True
    assert repeated["mode"] == "same_plan_second_apply"
    assert len(completed["prior_terminal_receipts"]) == 2


@pytest.mark.parametrize(
    "tamper_kind",
    (
        "bytes_without_outer_hash",
        "semantic_with_rebound_outer_hash",
        "source_key_with_rebound_outer_hash",
        "missing_worker_count_with_rebound_outer_hash",
        "worker_scope_false_with_rebound_outer_hash",
        "missing_cycle_isolation_with_rebound_outer_hash",
        "missing_parent_death_guard_with_rebound_outer_hash",
        "wrong_worker_budget_with_rebound_outer_hash",
        "boolean_worker_budget_with_rebound_outer_hash",
        "missing_filesystem_sandbox_with_rebound_outer_hash",
        "coverage_reset_false_with_rebound_outer_hash",
        "legacy_inner_schema_with_rebound_outer_hash",
    ),
)
def test_same_plan_rejects_tampered_completed_inner_receipt(
    tmp_path: Path,
    tamper_kind: str,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    preview = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=preview["plan_hash"],
    )
    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        preview["plan_hash"],
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    inner_path = backup_dir / receipt["inner_receipt_filename"]
    inner = json.loads(inner_path.read_text(encoding="utf-8"))
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001

    if tamper_kind == "bytes_without_outer_hash":
        inner_path.write_text(
            json.dumps(inner, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        if tamper_kind == "semantic_with_rebound_outer_hash":
            inner["status"] = "prepared"
        elif tamper_kind == "source_key_with_rebound_outer_hash":
            capture = dict(inner["source_capture"])
            capture["forged-source"] = capture.pop("codex")
            inner["source_capture"] = capture
        elif tamper_kind == "missing_worker_count_with_rebound_outer_hash":
            inner.pop("raw_generation_worker_count")
        elif tamper_kind == "worker_scope_false_with_rebound_outer_hash":
            inner["raw_generation_worker_scope_verified"] = False
        elif tamper_kind == "missing_cycle_isolation_with_rebound_outer_hash":
            inner["cycles"][0].pop("worker_isolation")
        elif tamper_kind == "missing_parent_death_guard_with_rebound_outer_hash":
            inner["cycles"][0]["worker_isolation"].pop("parent_death_guard")
        elif tamper_kind == "wrong_worker_budget_with_rebound_outer_hash":
            inner["cycles"][0]["worker_isolation"]["max_seconds"] = 1
        elif tamper_kind == "boolean_worker_budget_with_rebound_outer_hash":
            inner["cycles"][0]["worker_isolation"]["max_seconds"] = True
        elif tamper_kind == "missing_filesystem_sandbox_with_rebound_outer_hash":
            inner["cycles"][0]["worker_isolation"].pop("filesystem_sandbox")
        elif tamper_kind == "coverage_reset_false_with_rebound_outer_hash":
            inner["coverage_state_reset"] = False
        else:
            inner["schema_version"] = "mnemos.agent_source_raw_reconciliation.v1"
        inner_path.write_text(
            json.dumps(inner, sort_keys=True),
            encoding="utf-8",
        )
        receipt["inner_receipt_sha256"] = reconciler._file_sha256(inner_path)  # noqa: SLF001
        receipt_path.write_text(
            json.dumps(receipt, sort_keys=True),
            encoding="utf-8",
        )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_binding_mismatch",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=preview["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001


def test_recovery_plan_binds_shared_parser_execution_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    first = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    original_hash = recovery_support._file_sha256  # noqa: SLF001

    def changed_shared_dependency(path: Path):
        if Path(path).name == "raw_event_identity.py":
            return "0" * 64
        return original_hash(path)

    monkeypatch.setattr(
        recovery_support,
        "_file_sha256",
        changed_shared_dependency,
    )
    second = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )

    assert first["plan_hash"] != second["plan_hash"]
    assert (
        first["native_artifact_snapshot"]["execution_dependency_hashes"][
            "core/sync_framework/raw_event_identity.py"
        ]
        != second["native_artifact_snapshot"]["execution_dependency_hashes"][
            "core/sync_framework/raw_event_identity.py"
        ]
    )


def test_recovery_plan_dependency_closure_includes_split_script_runtime() -> None:
    """Every script split executed by apply must remain part of the exact plan."""

    assert {
        "scripts/reconcile_agent_source_raw_capture.py",
        "scripts/agent_source_raw_migration_certification.py",
        "scripts/agent_source_raw_migration_runtime.py",
        "scripts/agent_source_raw_recovery_contract.py",
        "scripts/agent_source_raw_recovery_support.py",
        "scripts/agent_source_raw_reconciliation_cli.py",
        "scripts/agent_source_raw_reconciliation_support.py",
        "scripts/agent_source_raw_worker_runtime.py",
        "scripts/agent_source_raw_worker_sandbox.py",
    } <= set(recovery_support.RECOVERY_EXECUTION_DEPENDENCY_PATHS)


def test_new_exact_plan_allows_a_legitimate_existing_raw_replay(tmp_path: Path):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    first_plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "first-backups",
        source=source,
    )
    reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=tmp_path / "first-backups",
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=first_plan["plan_hash"],
    )
    second_plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "second-backups",
        source=source,
    )

    replay = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=tmp_path / "second-backups",
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=second_plan["plan_hash"],
    )

    assert replay["ok"] is True, replay
    assert replay["first_apply"]["conservation_ok"] is True


def test_new_exact_plan_allows_a_revision_bound_content_change(tmp_path: Path):
    class _MutableSource(_Source):
        def parse_turns(self, _path: Path):
            return [
                Turn(
                    turn_number=0,
                    user_content=self.path.read_text(encoding="utf-8"),
                    assistant_content="synthetic-assistant",
                    native_event_id="synthetic-native-0",
                )
            ]

    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("first-content", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _MutableSource(source_path)
    first_plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "first-backups",
        source=source,
    )
    reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=tmp_path / "first-backups",
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=first_plan["plan_hash"],
    )
    source_path.write_text("second-content", encoding="utf-8")
    before_change = reconciler._safe_raw_conservation(raw_path)  # noqa: SLF001
    second_plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "second-backups",
        source=source,
    )

    changed = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=tmp_path / "second-backups",
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=second_plan["plan_hash"],
    )

    with _sqlite_transaction(raw_path) as connection:
        revision_count = int(
            connection.execute("SELECT COUNT(*) FROM raw_turn_revisions").fetchone()[0]
        )
    after_change = reconciler._safe_raw_conservation(raw_path)  # noqa: SLF001
    assert changed["ok"] is True, changed
    assert revision_count == 2
    assert reconciler._compare_raw_conservation(  # noqa: SLF001
        before_change,
        after_change,
    )

    with _sqlite_transaction(raw_path) as connection:
        effective_row = connection.execute(
            "SELECT metadata_json, completeness_status, updated_at FROM raw_turns"
        ).fetchone()
        assert effective_row is not None
        metadata = json.loads(str(effective_row[0] or "{}"))
        metadata["tampered_non_acl"] = True
        connection.execute(
            """
            UPDATE raw_turns
            SET metadata_json=?, completeness_status='partial',
                updated_at='tampered'
            """,
            (json.dumps(metadata),),
        )
    effective_tampered = reconciler._safe_raw_conservation(raw_path)  # noqa: SLF001
    assert (
        reconciler._compare_raw_conservation(  # noqa: SLF001
            before_change,
            effective_tampered,
        )
        is False
    )

    with _sqlite_transaction(raw_path) as connection:
        connection.execute(
            """
            UPDATE raw_turns
            SET metadata_json=?, completeness_status=?, updated_at=?
            """,
            effective_row,
        )
        connection.execute("""
            UPDATE raw_turns
            SET origin='tampered', source_path='tampered',
                user_content_blob=X'00'
            """)
    tampered = reconciler._safe_raw_conservation(raw_path)  # noqa: SLF001
    assert (
        reconciler._compare_raw_conservation(  # noqa: SLF001
            before_change,
            tampered,
        )
        is False
    )


def test_apply_rebuilds_a_host_raw_denominator_with_two_raw_only_generations(tmp_path: Path):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=source,
    )

    result = reconciler._execute_unresolved_active_source_raw_capture_for_test(  # noqa: SLF001
        config=config,
        raw_db_path=raw_path,
        backup_dir=tmp_path / "backups",
        sources=[source],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )

    assert result["ok"] is True, result
    assert result["after_challenger"]["ok"] is True
    assert result["source_capture"]["codex"]["ok"] is True
    assert len(result["cycles"]) == 2
    assert result["raw_only_boundary_ok"] is True
    assert (tmp_path / "sync_log.db").exists() is False
    assert all(item["integrity"] == "ok" for item in result["backups"].values() if item["present"])


def test_cursor_operations_close_database_descriptors_before_worker_fork(
    tmp_path: Path,
) -> None:
    cursor_path = tmp_path / "agent_sync_cursors.db"
    store = AgentSyncCursorStore(tmp_path)

    store.reset_source_reconciliation("codex")

    opened = {
        str(Path(item.path).resolve(strict=False))
        for item in reconciler.psutil.Process().open_files()
    }
    assert str(cursor_path.resolve(strict=False)) not in opened
    assert str(Path(f"{cursor_path}-wal").resolve(strict=False)) not in opened
    assert str(Path(f"{cursor_path}-shm").resolve(strict=False)) not in opened


def test_raw_generation_rejects_parent_database_handle_before_fork(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "raw_events.db"
    connection = sqlite3.connect(raw_path)
    try:
        with pytest.raises(
            reconciler.AgentSourceRawReconciliationError,
            match="raw_generation_parent_database_handle_open",
        ):
            reconciler._assert_raw_generation_parent_handles_closed(  # noqa: SLF001
                raw_db_path=raw_path,
                cursor_path=tmp_path / "agent_sync_cursors.db",
                coverage_path=tmp_path / "agent_source_coverage.json",
            )
    finally:
        connection.close()


def test_recovery_rejects_raw_database_outside_scoped_root(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    outside_raw = tmp_path.parent / f"{tmp_path.name}-outside-raw-events.db"

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_database_scope_mismatch",
    ):
        reconciler._reconcile_active_source_raw_capture_unlocked(  # noqa: SLF001
            config=_Config(tmp_path),
            raw_db_path=outside_raw,
            backup_dir=tmp_path / "backups",
            sources=[_Source(source_path)],
            apply=False,
            require_all_active_sources=False,
        )

    assert not outside_raw.exists()
    assert not (tmp_path / "backups").exists()


def test_public_apply_attributes_disjoint_external_database_write_without_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    trigger_path = backup_dir / "write-external.trigger"
    external_process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import pathlib,sqlite3,sys,time;"
                "trigger=pathlib.Path(sys.argv[1]);"
                "\nwhile not trigger.exists(): time.sleep(0.01);"
                "\nc=sqlite3.connect(sys.argv[2]);"
                "c.execute('CREATE TABLE IF NOT EXISTS external_events(id INTEGER)');"
                "c.execute('INSERT INTO external_events VALUES (1)');"
                "c.commit();c.close()"
            ),
            str(trigger_path),
            str(tmp_path / "events.db"),
        ]
    )
    real_run_generation = reconciler._run_raw_generation_isolated  # noqa: SLF001
    injected = False

    def run_with_external_event_write(**kwargs):
        nonlocal injected
        if not injected:
            injected = True
            trigger_path.touch()
            assert external_process.wait(timeout=10) == 0
        return real_run_generation(**kwargs)

    monkeypatch.setattr(
        reconciler,
        "_run_raw_generation_isolated",
        run_with_external_event_write,
    )

    try:
        result = reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )
    finally:
        if external_process.poll() is None:
            external_process.terminate()
            external_process.wait(timeout=10)

    expected_hash = hashlib.sha256(b"events.db").hexdigest()[:16]
    assert result["ok"] is True, result
    assert result["raw_only_boundary_ok"] is True
    assert result["process_write_scope_verified"] is True
    assert result["blocked_process_mutation_count"] == 0
    assert result["foreign_concurrent_mutation_count"] == 1
    assert result["foreign_concurrent_mutation_name_hashes"] == [expected_hash]
    with _sqlite_transaction(tmp_path / "events.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM external_events").fetchone()[0] == 1


def test_public_apply_blocks_apply_owned_exec_child_database_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    real_run_service = reconciler.raw_sync.run_service

    def run_with_owned_child(*args, **kwargs):
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sqlite3,sys;"
                        "c=sqlite3.connect(sys.argv[1]);"
                        "c.execute('CREATE TABLE forbidden(id INTEGER)');"
                        "c.commit();c.close()"
                    ),
                    str(tmp_path / "events.db"),
                ],
                check=True,
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("apply-owned child process was not blocked")
        return real_run_service(*args, **kwargs)

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        run_with_owned_child,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_generation_worker_write_scope_violation",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    assert not (tmp_path / "events.db").exists()
    markers = list(backup_dir.glob("agent-source-raw-reconciliation-2*.json"))
    assert len(markers) == 1
    marker = json.loads(markers[0].read_text(encoding="utf-8"))
    assert marker["raw_only_boundary_ok"] is False
    assert marker["blocked_process_mutation_count"] == 1


def test_process_write_scope_rejects_database_directory_metadata_mutation(
    tmp_path: Path,
) -> None:
    scope = reconciler._ProcessDatabaseWriteScope(  # noqa: SLF001
        database_dir=tmp_path,
        allowed_names={"raw_events.db"},
    )
    scope.start()
    try:
        with pytest.raises(
            PermissionError,
            match="raw_reconciliation_process_write_scope_violation",
        ):
            os.chmod(tmp_path, 0o700)
    finally:
        scope.close()

    assert scope.evidence()["blocked_process_mutation_count"] == 1


def test_process_write_scope_rejects_unowned_database_file(
    tmp_path: Path,
) -> None:
    scope = reconciler._ProcessDatabaseWriteScope(  # noqa: SLF001
        database_dir=tmp_path,
        allowed_names={"raw_events.db"},
    )

    with pytest.raises(
        PermissionError,
        match="raw_reconciliation_process_write_scope_violation",
    ):
        scope.authorize(tmp_path / "events.db")

    assert scope.evidence()["blocked_process_mutation_count"] == 1
    assert scope.evidence()["blocked_process_mutation_name_hashes"] == [
        hashlib.sha256(b"events.db").hexdigest()[:16]
    ]


def test_process_write_scope_rejects_apply_owned_exec_child(
    tmp_path: Path,
) -> None:
    scope = reconciler._ProcessDatabaseWriteScope(  # noqa: SLF001
        database_dir=tmp_path,
        allowed_names={"raw_events.db"},
    )
    scope.start()
    try:
        with pytest.raises(
            PermissionError,
            match="raw_reconciliation_process_exec_scope_violation",
        ):
            subprocess.run(
                [sys.executable, "-c", "pass"],
                check=True,
            )
    finally:
        scope.close()

    assert scope.evidence()["blocked_process_mutation_count"] == 1
    assert scope.evidence()["blocked_process_mutation_name_hashes"] == [
        hashlib.sha256(b"<exec-child>").hexdigest()[:16]
    ]


def test_audit_write_path_resolves_relative_dir_fd_on_macos_and_linux(
    tmp_path: Path,
) -> None:
    owned_root = tmp_path / "owned"
    owned_root.mkdir()
    descriptor = os.open(owned_root, os.O_RDONLY)
    try:
        paths = recovery_support._audit_event_write_paths(  # noqa: SLF001
            "os.remove",
            ("turn-spool.ndjson", descriptor),
        )
    finally:
        os.close(descriptor)

    assert paths == ((owned_root / "turn-spool.ndjson").resolve(),)


def test_process_write_scope_blocks_unattributed_relative_open_with_dir_fd(
    tmp_path: Path,
) -> None:
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    descriptor = os.open(database_dir, os.O_RDONLY)
    scope = reconciler._ProcessDatabaseWriteScope(  # noqa: SLF001
        database_dir=database_dir,
        allowed_names={"raw_events.db"},
    )
    scope.start()
    try:
        with pytest.raises(
            PermissionError,
            match="raw_reconciliation_process_relative_open_scope_violation",
        ):
            os.open(
                "unowned.db",
                os.O_CREAT | os.O_WRONLY,
                0o600,
                dir_fd=descriptor,
            )
    finally:
        scope.close()
        os.close(descriptor)

    assert not (database_dir / "unowned.db").exists()
    assert scope.evidence()["blocked_process_mutation_name_hashes"] == [
        hashlib.sha256(b"<ambiguous-relative-open>").hexdigest()[:16]
    ]


def test_process_write_scope_blocks_write_event_with_unresolvable_descriptor(
    tmp_path: Path,
) -> None:
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    scope = reconciler._ProcessDatabaseWriteScope(  # noqa: SLF001
        database_dir=database_dir,
        allowed_names={"raw_events.db"},
    )
    scope.start()
    try:
        with pytest.raises(
            PermissionError,
            match="raw_reconciliation_process_unattributed_write_scope_violation",
        ):
            recovery_support._process_write_audit_hook(  # noqa: SLF001
                "os.remove",
                ("unowned.db", 999_999),
            )
    finally:
        scope.close()

    assert scope.evidence()["blocked_process_mutation_name_hashes"] == [
        hashlib.sha256(b"<unattributed-write>").hexdigest()[:16]
    ]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
@pytest.mark.parametrize("guard_kind", ("raw_generation", "challenger"))
def test_worker_guard_blocks_relative_dir_fd_write_and_chdir(
    tmp_path: Path,
    guard_kind: str,
) -> None:
    worker_root = tmp_path / "worker"
    external_root = tmp_path / "external"
    worker_root.mkdir()
    external_root.mkdir()
    pid = os.fork()
    if pid == 0:
        try:
            os.chdir(worker_root)
            if guard_kind == "raw_generation":
                reconciler._install_raw_generation_write_guard(  # noqa: SLF001
                    database_dir=tmp_path / "database",
                    allowed_names={"raw_events.db"},
                    allowed_write_roots=(worker_root,),
                    blocked_name_hashes=set(),
                )
            else:
                reconciler._install_challenger_read_only_guard(  # noqa: SLF001
                    allowed_write_roots=(worker_root,),
                    blocked_name_hashes=set(),
                )
            descriptor = os.open(external_root, os.O_RDONLY)
            try:
                with pytest.raises(PermissionError):
                    os.open(
                        "outside.db",
                        os.O_CREAT | os.O_WRONLY,
                        0o600,
                        dir_fd=descriptor,
                    )
            finally:
                os.close(descriptor)
            with pytest.raises(PermissionError):
                os.chdir(external_root)
            os._exit(0)
        except BaseException:
            os._exit(1)

    waited_pid, status = os.waitpid(pid, 0)
    assert waited_pid == pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert not (external_root / "outside.db").exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_recovery_worker_registry_reaps_a_dead_controller_root() -> None:
    path_read, path_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(path_read)
            stale_root, _cleaned = recovery_support._create_recovery_worker_root(  # noqa: SLF001
                "raw-generation"
            )
            os.write(path_write, str(stale_root).encode("utf-8"))
            os._exit(0)
        except BaseException:
            os._exit(1)

    os.close(path_write)
    stale_root = Path(os.read(path_read, 4096).decode("utf-8"))
    os.close(path_read)
    waited_pid, status = os.waitpid(pid, 0)
    assert waited_pid == pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert stale_root.is_dir()

    replacement_root, cleaned = recovery_support._create_recovery_worker_root(  # noqa: SLF001
        "challenger"
    )
    try:
        assert cleaned >= 1
        assert not stale_root.exists()
    finally:
        reconciler._remove_challenger_worker_root(replacement_root)  # noqa: SLF001


def test_recovery_worker_registry_removes_unpublished_worker_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_root = recovery_support._safe_recovery_worker_registry_root()  # noqa: SLF001
    before = {
        child.name
        for child in registry_root.iterdir()
        if child.name.startswith(("challenger-", "raw-generation-"))
    }

    def fail_owner_publish(_source: Path, _target: Path) -> None:
        raise OSError("injected owner publication failure")

    monkeypatch.setattr(recovery_support.os, "replace", fail_owner_publish)

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="recovery_worker_registry_unavailable",
    ):
        recovery_support._create_recovery_worker_root("challenger")  # noqa: SLF001

    after = {
        child.name
        for child in registry_root.iterdir()
        if child.name.startswith(("challenger-", "raw-generation-"))
    }
    assert after == before


def test_challenger_kernel_sandbox_blocks_c_writes_metadata_and_inherited_fd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=_Config(tmp_path)).close()
    external_root = tmp_path.parent / f"{tmp_path.name}-challenger-sandbox"
    external_root.mkdir()
    external_file = external_root / "external.txt"
    external_file.write_bytes(b"before")
    external_mtime_ns = external_file.stat().st_mtime_ns
    attached_path = external_root / "attached.db"
    fifo_path = external_root / "escaped.fifo"
    inherited_descriptor = os.open(external_file, os.O_WRONLY)
    real_audit = reconciler.audit_native_to_raw

    def require_blocked(operation) -> None:
        try:
            operation()
        except (OSError, sqlite3.Error):
            return
        raise AssertionError("kernel worker sandbox allowed an external write")

    def attempt_external_writes(*args, **kwargs):
        assert (
            recovery_support._audit_event_path(inherited_descriptor)  # noqa: SLF001
            != external_file.resolve()
        )
        require_blocked(
            lambda: os.utime(
                external_file,
                ns=(external_mtime_ns + 1, external_mtime_ns + 1),
            )
        )
        if hasattr(os, "mkfifo"):
            require_blocked(lambda: os.mkfifo(fifo_path))

        def attach_external_database() -> None:
            connection = sqlite3.connect(kwargs["raw_db_path"])
            try:
                connection.execute(f"ATTACH DATABASE {json.dumps(str(attached_path))} AS pwn")
                connection.execute("CREATE TABLE pwn.payload(value TEXT)")
                connection.commit()
            finally:
                connection.close()

        require_blocked(attach_external_database)
        return real_audit(*args, **kwargs)

    monkeypatch.setattr(
        reconciler,
        "audit_native_to_raw",
        attempt_external_writes,
    )
    try:
        with pytest.raises(
            reconciler.AgentSourceRawReconciliationError,
            match="native_challenger_write_scope_violation",
        ):
            reconciler.reconcile_active_source_raw_capture(
                config=_Config(tmp_path),
                raw_db_path=raw_path,
                backup_dir=tmp_path / "backups",
                sources=[_Source(source_path)],
                apply=False,
                cycles=2,
                require_all_active_sources=False,
                runtime_writers_are_inactive=lambda: True,
            )
    finally:
        os.close(inherited_descriptor)

    assert external_file.read_bytes() == b"before"
    assert external_file.stat().st_mtime_ns == external_mtime_ns
    assert not attached_path.exists()
    assert not fifo_path.exists()


def test_raw_generation_kernel_sandbox_blocks_c_writes_and_inherited_fd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    external_root = tmp_path.parent / f"{tmp_path.name}-generation-sandbox"
    external_root.mkdir()
    external_file = external_root / "external.txt"
    external_file.write_bytes(b"before")
    external_mtime_ns = external_file.stat().st_mtime_ns
    attached_path = external_root / "attached.db"
    fifo_path = external_root / "escaped.fifo"
    inherited_descriptor = os.open(external_file, os.O_WRONLY)
    real_run_service = reconciler.raw_sync.run_service

    def require_blocked(operation) -> None:
        try:
            operation()
        except (OSError, sqlite3.Error):
            return
        raise AssertionError("kernel worker sandbox allowed an external write")

    def run_with_external_write_attempts(*args, **kwargs):
        assert (
            recovery_support._audit_event_path(inherited_descriptor)  # noqa: SLF001
            != external_file.resolve()
        )
        require_blocked(
            lambda: os.utime(
                external_file,
                ns=(external_mtime_ns + 1, external_mtime_ns + 1),
            )
        )
        if hasattr(os, "mkfifo"):
            require_blocked(lambda: os.mkfifo(fifo_path))

        def attach_external_database() -> None:
            connection = sqlite3.connect(raw_path)
            try:
                connection.execute(f"ATTACH DATABASE {json.dumps(str(attached_path))} AS pwn")
                connection.execute("CREATE TABLE pwn.payload(value TEXT)")
                connection.commit()
            finally:
                connection.close()

        require_blocked(attach_external_database)
        return real_run_service(*args, **kwargs)

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        run_with_external_write_attempts,
    )
    try:
        applied = reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )
    finally:
        os.close(inherited_descriptor)

    assert applied["ok"] is True
    assert external_file.read_bytes() == b"before"
    assert external_file.stat().st_mtime_ns == external_mtime_ns
    assert not attached_path.exists()
    assert not fifo_path.exists()


def test_public_apply_rejects_backup_scope_equal_to_database_root_before_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    monkeypatch.setattr(
        reconciler,
        "_ensure_private_backup_dir",
        lambda _path: (_ for _ in ()).throw(AssertionError("backup initialization must not run")),
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="backup_scope_overlaps_database",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=tmp_path,
            sources=[],
            apply=True,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash="sha256:" + ("a" * 64),
        )


def test_process_write_scope_fails_closed_when_descriptor_flags_are_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import fcntl

    event_path = tmp_path / "events.db"
    connection = sqlite3.connect(event_path)
    connection.execute("CREATE TABLE existing(id INTEGER)")
    connection.commit()
    monkeypatch.setattr(
        fcntl,
        "fcntl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("descriptor unavailable")),
    )
    scope = reconciler._ProcessDatabaseWriteScope(  # noqa: SLF001
        database_dir=tmp_path,
        allowed_names={"raw_events.db"},
    )
    try:
        with pytest.raises(
            reconciler.AgentSourceRawReconciliationError,
            match="process_write_scope_descriptor_audit_failed",
        ):
            scope.start()
    finally:
        scope.close()
        connection.close()

    assert scope.evidence()["process_write_scope_verified"] is False


def test_public_apply_blocks_owned_foreign_database_write_and_preserves_failure_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    real_run_service = reconciler.raw_sync.run_service

    def run_with_owned_event_write(*args, **kwargs):
        try:
            with _sqlite_transaction(tmp_path / "events.db") as connection:
                connection.execute("CREATE TABLE forbidden(id INTEGER)")
        except PermissionError:
            pass
        else:
            raise AssertionError("owned foreign write was not blocked")
        return real_run_service(*args, **kwargs)

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        run_with_owned_event_write,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_generation_worker_write_scope_violation",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    assert not (tmp_path / "events.db").exists()
    inner_paths = list(backup_dir.glob("agent-source-raw-reconciliation-2*.json"))
    assert len(inner_paths) == 1
    marker = json.loads(inner_paths[0].read_text(encoding="utf-8"))
    expected_hash = hashlib.sha256(b"events.db").hexdigest()[:16]
    assert marker["status"] == "rolled_back_by_migration_certification"
    assert marker["raw_only_boundary_ok"] is False
    assert marker["process_write_scope_verified"] is True
    assert marker["blocked_process_mutation_count"] == 1
    assert marker["blocked_process_mutation_name_hashes"] == [expected_hash]
    archive_path = backup_dir / marker["invalidated_receipt_filename"]
    archive_bytes = archive_path.read_bytes()
    assert hashlib.sha256(archive_bytes).hexdigest() == (
        marker["invalidated_receipt_sha256"].removeprefix("sha256:")
    )
    archived = json.loads(archive_bytes)
    assert archived["status"] == "failed"
    assert archived["blocked_process_mutation_name_hashes"] == [expected_hash]


def test_public_apply_rejects_preopened_owned_foreign_database_handle(
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    event_path = tmp_path / "events.db"
    connection = sqlite3.connect(event_path)
    connection.execute("CREATE TABLE existing(id INTEGER)")
    connection.commit()
    event_bytes_before = event_path.read_bytes()
    try:
        with pytest.raises(
            reconciler.AgentSourceRawReconciliationError,
            match="process_write_scope_preexisting_handle",
        ):
            reconciler.reconcile_active_source_raw_capture(
                config=config,
                raw_db_path=raw_path,
                backup_dir=backup_dir,
                sources=[source],
                apply=True,
                cycles=2,
                require_all_active_sources=False,
                runtime_writers_are_inactive=lambda: True,
                expected_plan_hash=plan["plan_hash"],
            )
    finally:
        connection.close()

    assert event_path.read_bytes() == event_bytes_before
    inner_paths = list(backup_dir.glob("agent-source-raw-reconciliation-2*.json"))
    assert len(inner_paths) == 1
    marker = json.loads(inner_paths[0].read_text(encoding="utf-8"))
    assert marker["status"] == "rolled_back_by_migration_certification"
    assert marker["raw_only_boundary_ok"] is False
    assert marker["blocked_process_mutation_count"] == 1
    assert marker["blocked_process_mutation_name_hashes"] == [
        hashlib.sha256(b"events.db").hexdigest()[:16]
    ]


def test_rollback_invalidation_rejects_symlinked_inner_receipt(
    tmp_path: Path,
) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    external = tmp_path / "outside.json"
    external.write_text('{"status":"completed"}\n', encoding="utf-8")
    os.chmod(external, 0o600)
    receipt_name = "agent-source-raw-reconciliation-20260726T000000000000Z.json"
    (backup_dir / receipt_name).symlink_to(external)

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="rollback_receipt_source_unsafe",
    ):
        reconciler._mark_reconciliation_receipt_rolled_back(  # noqa: SLF001
            backup_dir=backup_dir,
            applied={"receipt_filename": receipt_name},
        )

    assert external.read_text(encoding="utf-8") == '{"status":"completed"}\n'
    assert not list(backup_dir.glob("agent-source-raw-reconciliation-invalidated.*.json"))


def test_private_receipt_read_rejects_leaf_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    receipt = backup_dir / "receipt.json"
    receipt.write_bytes(b'{"status":"prepared"}\n')
    os.chmod(receipt, 0o600)
    replacement = backup_dir / "replacement.json"
    replacement.write_bytes(b'{"status":"counterfeit"}\n')
    os.chmod(replacement, 0o600)
    original_read = recovery_support.secure_read_bytes

    def replace_after_read(root: Path, relative_path: str | Path):
        content = original_read(root, relative_path)
        os.replace(replacement, receipt)
        return content

    monkeypatch.setattr(
        recovery_support,
        "secure_read_bytes",
        replace_after_read,
    )

    with pytest.raises(
        OSError,
        match="private_backup_file_changed",
    ):
        reconciler._read_private_backup_bytes(  # noqa: SLF001
            receipt,
            backup_dir,
        )

    assert receipt.read_bytes() == b'{"status":"counterfeit"}\n'


def test_private_receipt_read_rejects_hardlinked_receipt(
    tmp_path: Path,
) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    receipt = backup_dir / "receipt.json"
    receipt.write_bytes(b'{"status":"prepared"}\n')
    os.chmod(receipt, 0o600)
    outside_link = tmp_path / "receipt-hardlink.json"
    os.link(receipt, outside_link)

    with pytest.raises(
        OSError,
        match="private_backup_file_changed",
    ):
        reconciler._read_private_backup_bytes(  # noqa: SLF001
            receipt,
            backup_dir,
        )

    assert outside_link.read_bytes() == b'{"status":"prepared"}\n'


def test_apply_closes_an_explicit_verified_empty_active_source(tmp_path: Path):
    config = _Config(tmp_path)
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    empty_root = tmp_path / "empty-native-root"
    empty_root.mkdir()
    source = _EmptySource(empty_root)
    backup_dir = tmp_path / "backups"
    plan = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=False,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
    )

    result = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )

    capture = result["source_capture"]["aider"]
    assert result["ok"] is True, result
    assert capture["ok"] is True, capture
    assert capture["capture_completeness"]["native_turns"] == 0
    assert capture["capture_completeness"]["discovered_sessions"] == 0


def test_apply_refuses_to_reset_derived_cursors_when_daemon_is_not_proven_inactive(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    before = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    source = _Source(source_path)
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=source,
        writers_inactive=False,
    )

    with pytest.raises(reconciler.AgentSourceRawReconciliationError, match="daemon_not_inactive"):
        reconciler._execute_unresolved_active_source_raw_capture_for_test(  # noqa: SLF001
            config=config,
            raw_db_path=raw_path,
            backup_dir=tmp_path / "backups",
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: False,
            expected_plan_hash=plan["plan_hash"],
        )

    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == before


def test_apply_rejects_native_source_drift_before_backup(tmp_path: Path):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=source,
    )
    source_path.write_text("synthetic-changed", encoding="utf-8")

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="expected_plan_hash_mismatch",
    ):
        reconciler._execute_unresolved_active_source_raw_capture_for_test(  # noqa: SLF001
            config=config,
            raw_db_path=raw_path,
            backup_dir=tmp_path / "backups",
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )
    assert not (tmp_path / "backups").exists()


def test_public_apply_rolls_back_native_drift_detected_after_snapshot_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    original_post_gap = reconciler._post_apply_raw_gap  # noqa: SLF001

    def drift_then_verify(**kwargs):
        source_path.write_text("changed-after-artifact-snapshot", encoding="utf-8")
        return original_post_gap(**kwargs)

    monkeypatch.setattr(reconciler, "_post_apply_raw_gap", drift_then_verify)

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="post_apply_native_snapshot_drift",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001


def test_public_apply_restores_all_targets_when_recovery_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001

    def fail_after_mutating_all_targets(
        *_args,
        coverage_state_sink,
        **_kwargs,
    ):
        with _sqlite_transaction(raw_path) as connection:
            connection.execute("CREATE TABLE rollback_probe (probe_id TEXT PRIMARY KEY)")
        coverage_state_sink({"schema_version": "rollback-probe"})
        raise RuntimeError("boom")

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        fail_after_mutating_all_targets,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_generation_worker_failed",
    ) as captured:
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=tmp_path / "backups",
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    failure = captured.value.details["worker_failure"]
    assert failure == {
        "schema_version": "mnemos.raw_generation_worker_failure.v3",
        "exception_type": "RuntimeError",
        "reason_code": "",
        "failure_phase": "run_raw_service",
        "guardian_exit_code": None,
        "os_errno": None,
        "sqlite_errorcode": None,
        "sqlite_errorname": "",
    }
    assert "boom" not in json.dumps(failure, sort_keys=True)


def test_inherited_regular_fd_retirement_cannot_close_a_reused_guard_pipe(
    tmp_path: Path,
) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("requires fork")
    sentinel = tmp_path / "inherited.log"
    pid = os.fork()
    if pid == 0:
        try:
            inherited = sentinel.open("wb")
            inherited_fd = inherited.fileno()
            reconciler._close_inherited_regular_file_descriptors()  # noqa: SLF001
            pipe_read, pipe_write = os.pipe()
            if inherited_fd in {pipe_read, pipe_write}:
                os._exit(71)
            inherited.close()
            os.write(pipe_write, b"x")
            if os.read(pipe_read, 1) != b"x":
                os._exit(72)
            os.close(pipe_read)
            os.close(pipe_write)
            os._exit(0)
        except BaseException:
            os._exit(73)
    waited_pid, status = os.waitpid(pid, 0)
    assert waited_pid == pid
    assert os.waitstatus_to_exitcode(status) == 0


def test_process_write_scope_allows_only_exact_restore_sidecars(
    tmp_path: Path,
) -> None:
    scope = recovery_support._ProcessDatabaseWriteScope(  # noqa: SLF001
        database_dir=tmp_path,
        allowed_names={"raw_events.db"},
    )
    exact = tmp_path / ".raw_events.db.generation.restore-journal"
    assert scope._is_allowed(exact) is True  # noqa: SLF001
    assert (
        scope._is_allowed(tmp_path / ".foreign.db.generation.restore-journal")  # noqa: SLF001
        is False
    )


def test_public_apply_rolls_back_when_post_restore_drill_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    monkeypatch.setattr(
        reconciler,
        "_restore_drill_ok",
        lambda **_kwargs: False,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="backup_restore_drill_failed",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=tmp_path / "backups",
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001


def test_public_rollback_durably_removes_new_derived_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    config = _Config(database_dir)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = database_dir / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    rollback_started = False
    synced_after_failure: list[Path] = []
    real_fsync_directory = reconciler.fsync_directory

    def fail_post_gap(**_kwargs):
        nonlocal rollback_started
        rollback_started = True
        return {
            "schema_version": "mnemos.agent_source_raw_post_gap.v1",
            "required_gap": 1,
            "ok": False,
        }

    def record_fsync(path: Path) -> None:
        if rollback_started:
            synced_after_failure.append(Path(path).resolve())
        real_fsync_directory(path)

    monkeypatch.setattr(reconciler, "_post_apply_raw_gap", fail_post_gap)
    monkeypatch.setattr(recovery_support, "fsync_directory", record_fsync)

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="post_apply_gap_nonzero",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert not (database_dir / "agent_sync_cursors.db").exists()
    assert not reconciler.agent_source_coverage.coverage_state_path(database_dir).exists()
    assert database_dir.resolve() in synced_after_failure


def test_unlink_targets_durably_fsyncs_each_mutated_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_parent = tmp_path / "first"
    second_parent = tmp_path / "second"
    first_parent.mkdir()
    second_parent.mkdir()
    first = first_parent / "first.db"
    second = second_parent / "second.db"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    synced: list[Path] = []

    monkeypatch.setattr(
        recovery_support,
        "fsync_directory",
        lambda path: synced.append(Path(path).resolve()),
    )

    recovery_support._unlink_targets_durably(  # noqa: SLF001
        (first, second),
        error_code="injected_cleanup_failed",
    )

    assert not first.exists()
    assert not second.exists()
    assert synced == sorted((first_parent.resolve(), second_parent.resolve()))


@pytest.mark.parametrize(
    ("fault", "error_code"),
    (
        ("conservation", "first_apply_conservation_failed"),
        ("post_gap", "post_apply_gap_nonzero"),
        ("second_verify", "second_apply_receipt_missing"),
        ("evidence_write", "migration_evidence_write_failed"),
        ("rollback", "rollback_failed"),
    ),
)
def test_public_apply_certification_fault_matrix(
    fault: str,
    error_code: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001

    if fault == "conservation":
        monkeypatch.setattr(
            reconciler,
            "_compare_raw_conservation",
            lambda *_args, **_kwargs: False,
        )
    elif fault == "post_gap":
        monkeypatch.setattr(
            reconciler,
            "_post_apply_raw_gap",
            lambda **_kwargs: {
                "schema_version": "mnemos.agent_source_raw_post_gap.v1",
                "required_gap": 1,
                "ok": False,
            },
        )
    elif fault == "second_verify":
        monkeypatch.setattr(
            reconciler,
            "_verify_completed_raw_receipt",
            lambda **_kwargs: None,
        )
    elif fault == "evidence_write":
        real_write = reconciler._write_receipt  # noqa: SLF001

        def fail_outer_completion(path: Path, payload):
            if (
                path.name.startswith("agent-source-raw-migration.")
                and payload.get("status") == "completed"
            ):
                raise OSError("injected outer completion failure")
            return real_write(path, payload)

        monkeypatch.setattr(
            reconciler,
            "_write_receipt",
            fail_outer_completion,
        )
    elif fault == "rollback":
        monkeypatch.setattr(
            reconciler,
            "_restore_drill_ok",
            lambda **_kwargs: False,
        )

        def fail_restore(**_kwargs):
            raise reconciler.AgentSourceRawReconciliationError("injected_restore_failure")

        monkeypatch.setattr(
            reconciler,
            "_restore_recovery_state",
            fail_restore,
        )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match=error_code,
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        plan["plan_hash"],
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if fault == "rollback":
        assert receipt["status"] == "prepared"
        assert (
            reconciler._target_state(  # noqa: SLF001
                config,
                raw_path,
            )
            != before
        )
    else:
        assert (
            reconciler._target_state(  # noqa: SLF001
                config,
                raw_path,
            )
            == before
        )
        assert receipt["status"] == "recovered_rollback"
        assert receipt["rollback_ok"] is True
        if fault == "conservation":
            comparator = receipt["first_apply_comparator"]
            assert comparator["ok"] is False
            assert comparator["structural_findings"] == []
            assert comparator["conservation_findings"] == [
                {
                    "table": "__comparator__",
                    "rule": "boolean_finding_disagreement",
                    "mismatch_count": 1,
                }
            ]
        inner = json.loads(
            (backup_dir / receipt["inner_receipt_filename"]).read_text(encoding="utf-8")
        )
        assert inner["status"] == "rolled_back_by_migration_certification"


def test_public_apply_rejects_plan_drift_before_backup(
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    source_path.write_text("drifted-before-outer-apply", encoding="utf-8")

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="expected_plan_hash_mismatch",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert list(backup_dir.iterdir()) == []


def test_public_apply_builds_one_locked_plan_before_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    real_bind = reconciler._bind_recovery_plan  # noqa: SLF001
    bound_plan_hashes: list[str] = []

    def record_bound_plan(*args, **kwargs):
        result = real_bind(*args, **kwargs)
        bound_plan_hashes.append(str(result["plan_hash"]))
        return result

    monkeypatch.setattr(
        reconciler,
        "_bind_recovery_plan",
        record_bound_plan,
    )

    applied = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )

    assert applied["ok"] is True
    assert bound_plan_hashes == [plan["plan_hash"]]


def test_recovery_challengers_run_in_disposable_workers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    config = _Config(tmp_path)
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    worker_pid_read, worker_pid_write = os.pipe()
    real_audit = reconciler.audit_native_to_raw

    def record_worker_pid(*args, **kwargs):
        os.write(worker_pid_write, f"{os.getpid()}\n".encode("ascii"))
        return real_audit(*args, **kwargs)

    monkeypatch.setattr(
        reconciler,
        "audit_native_to_raw",
        record_worker_pid,
    )

    try:
        applied = reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )
    finally:
        os.close(worker_pid_write)
    worker_pids = [
        int(value) for value in os.read(worker_pid_read, 4096).decode("ascii").splitlines()
    ]
    os.close(worker_pid_read)
    assert applied["ok"] is True
    assert len(worker_pids) == 4
    assert all(pid != os.getpid() for pid in worker_pids)


def test_recovery_challenger_worker_failure_is_typed_before_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=_Config(tmp_path)).close()

    def terminate_worker(*_args, **_kwargs):
        os._exit(77)

    monkeypatch.setattr(
        reconciler,
        "audit_native_to_raw",
        terminate_worker,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_worker_failed",
    ):
        reconciler._audit_native_to_raw_isolated(  # noqa: SLF001
            [_Source(source_path)],
            raw_db_path=raw_path,
            require_all_host_sources=False,
            source_scope="active",
        )


def test_recovery_challenger_rejects_inconsistent_worker_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=_Config(tmp_path)).close()

    monkeypatch.setattr(
        reconciler,
        "audit_native_to_raw",
        lambda *_args, **_kwargs: {
            "schema_version": "mnemos.agent_source_native_raw_challenger.v3",
            "source_scope": "active",
            "sources": {"codex": {"status": "blocked", "errors": ["injected"]}},
            "blocking_sources": [],
            "ok": True,
        },
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_report_invalid",
    ):
        reconciler._audit_native_to_raw_isolated(  # noqa: SLF001
            [_Source(source_path)],
            raw_db_path=raw_path,
            require_all_host_sources=False,
            source_scope="active",
        )


def test_public_apply_blocks_challenger_formal_write_before_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    config = _Config(tmp_path)
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    raw_before = raw_path.read_bytes()

    def mutate_formal_raw(*_args, **kwargs):
        Path(kwargs["raw_db_path"]).write_bytes(b"MUTATED-BEFORE-BACKUP")
        raise AssertionError("formal Raw mutation should have been blocked")

    monkeypatch.setattr(
        reconciler,
        "audit_native_to_raw",
        mutate_formal_raw,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_worker_failed",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert raw_path.read_bytes() == raw_before
    assert list(backup_dir.iterdir()) == []


def test_challenger_reports_caught_write_attempt_instead_of_signing_green(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=_Config(tmp_path)).close()
    raw_before = raw_path.read_bytes()
    real_audit = reconciler.audit_native_to_raw

    def catch_forbidden_raw_write(*args, **kwargs):
        try:
            Path(kwargs["raw_db_path"]).write_bytes(b"forbidden")
        except PermissionError:
            pass
        else:
            raise AssertionError("challenger Raw write was not blocked")
        return real_audit(*args, **kwargs)

    monkeypatch.setattr(
        reconciler,
        "audit_native_to_raw",
        catch_forbidden_raw_write,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_write_scope_violation",
    ):
        reconciler._audit_native_to_raw_isolated(  # noqa: SLF001
            [_Source(source_path)],
            raw_db_path=raw_path,
            require_all_host_sources=False,
            source_scope="active",
        )

    assert raw_path.read_bytes() == raw_before


def test_challenger_blocks_snapshot_and_database_root_external_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=_Config(tmp_path)).close()
    outside_path = tmp_path.parent / f"{tmp_path.name}-challenger-external.db"
    real_audit = reconciler.audit_native_to_raw

    def catch_forbidden_writes(sources, *args, **kwargs):
        snapshot_source = list(sources)[0]
        snapshot_session = next(iter(snapshot_source._snapshot_sessions.values()))  # noqa: SLF001
        for target in (snapshot_session.source_path, outside_path):
            try:
                Path(target).write_bytes(b"forbidden")
            except PermissionError:
                pass
            else:
                raise AssertionError("challenger out-of-scope write was not blocked")
        return real_audit(sources, *args, **kwargs)

    monkeypatch.setattr(
        reconciler,
        "audit_native_to_raw",
        catch_forbidden_writes,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_write_scope_violation",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=_Config(tmp_path),
            raw_db_path=raw_path,
            backup_dir=tmp_path / "backups",
            sources=[_Source(source_path)],
            apply=False,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
        )

    assert source_path.read_text(encoding="utf-8") == "synthetic-safe"
    assert not outside_path.exists()


def test_recovery_challenger_worker_timeout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=_Config(tmp_path)).close()
    monkeypatch.setattr(
        reconciler,
        "_CHALLENGER_WORKER_MAX_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        reconciler,
        "audit_native_to_raw",
        lambda *_args, **_kwargs: time.sleep(10),
    )

    started = time.monotonic()
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_worker_timeout",
    ):
        reconciler._audit_native_to_raw_isolated(  # noqa: SLF001
            [_Source(source_path)],
            raw_db_path=raw_path,
            require_all_host_sources=False,
            source_scope="active",
        )

    assert time.monotonic() - started < 2


def test_recovery_challenger_worker_budgets_and_cleanup_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=_Config(tmp_path)).close()
    source = _Source(source_path)
    real_audit = reconciler.audit_native_to_raw

    monkeypatch.setattr(reconciler, "_CHALLENGER_WORKER_MAX_RSS_BYTES", -1)
    monkeypatch.setattr(
        reconciler,
        "audit_native_to_raw",
        lambda *_args, **_kwargs: time.sleep(10),
    )
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_worker_budget_exceeded",
    ):
        reconciler._audit_native_to_raw_isolated(  # noqa: SLF001
            [source],
            raw_db_path=raw_path,
            require_all_host_sources=False,
            source_scope="active",
        )

    monkeypatch.setattr(reconciler, "_CHALLENGER_WORKER_MAX_RSS_BYTES", 1024**3)
    monkeypatch.setattr(reconciler, "_CHALLENGER_WORKER_MAX_REPORT_BYTES", 1)
    monkeypatch.setattr(
        reconciler,
        "audit_native_to_raw",
        real_audit,
    )
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_report_budget_exceeded",
    ):
        reconciler._audit_native_to_raw_isolated(  # noqa: SLF001
            [source],
            raw_db_path=raw_path,
            require_all_host_sources=False,
            source_scope="active",
        )

    worker_root = tmp_path.parent / f"{tmp_path.name}-challenger-cleanup"
    real_rmtree = reconciler.shutil.rmtree

    def make_worker_root(*_args, **_kwargs):
        worker_root.mkdir(mode=0o700)
        return str(worker_root)

    monkeypatch.setattr(reconciler.tempfile, "mkdtemp", make_worker_root)
    monkeypatch.setattr(reconciler, "_CHALLENGER_WORKER_MAX_REPORT_BYTES", 1024**2)
    monkeypatch.setattr(reconciler.shutil, "rmtree", lambda *_args, **_kwargs: None)
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_cleanup_failed",
    ):
        reconciler._audit_native_to_raw_isolated(  # noqa: SLF001
            [source],
            raw_db_path=raw_path,
            require_all_host_sources=False,
            source_scope="active",
        )
    real_rmtree(worker_root)


def test_public_apply_runs_each_raw_generation_in_disposable_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    worker_pid_read, worker_pid_write = os.pipe()
    real_run_service = reconciler.raw_sync.run_service

    def record_worker_pid(*args, **kwargs):
        os.write(worker_pid_write, f"{os.getpid()}\n".encode("ascii"))
        return real_run_service(*args, **kwargs)

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        record_worker_pid,
    )

    try:
        applied = reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )
    finally:
        os.close(worker_pid_write)
    worker_pids = [
        int(value) for value in os.read(worker_pid_read, 4096).decode("ascii").splitlines()
    ]
    os.close(worker_pid_read)
    assert applied["ok"] is True
    assert len(worker_pids) == 2
    assert len(set(worker_pids)) == 2
    assert all(pid != os.getpid() for pid in worker_pids)
    outer = json.loads(
        reconciler._migration_receipt_path(  # noqa: SLF001
            backup_dir,
            plan["plan_hash"],
        ).read_text(encoding="utf-8")
    )
    inner = json.loads((backup_dir / outer["inner_receipt_filename"]).read_text(encoding="utf-8"))
    assert inner["raw_generation_worker_count"] == 2
    assert all(
        cycle["worker_isolation"]["schema_version"] == "mnemos.raw_generation_worker_isolation.v1"
        for cycle in inner["cycles"]
    )


def test_public_apply_raw_generation_crash_is_typed_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        lambda *_args, **_kwargs: os._exit(77),
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_generation_worker_failed",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    outer = json.loads(
        reconciler._migration_receipt_path(  # noqa: SLF001
            backup_dir,
            plan["plan_hash"],
        ).read_text(encoding="utf-8")
    )
    assert outer["status"] == "recovered_rollback"
    inner = json.loads((backup_dir / outer["inner_receipt_filename"]).read_text(encoding="utf-8"))
    assert inner["schema_version"] == reconciler.SCHEMA_VERSION
    assert inner["status"] == "rolled_back_by_migration_certification"
    assert inner["rollback_ok"] is True


def test_raw_generation_worker_dies_when_controller_is_sigkilled(
    tmp_path: Path,
) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("requires fork")
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    worker_pid_read, worker_pid_write = os.pipe()
    guardian_pid_read, guardian_pid_write = os.pipe()
    worker_root_read, worker_root_write = os.pipe()
    completion_read, completion_write = os.pipe()
    context = multiprocessing.get_context("fork")

    def controller_entry() -> None:
        real_run_service = reconciler.raw_sync.run_service
        real_mkdtemp = reconciler.tempfile.mkdtemp
        real_establish_guard = reconciler._establish_parent_death_guard  # noqa: SLF001

        def record_worker_root(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            if str(kwargs.get("prefix") or "").startswith("raw-generation-"):
                os.write(
                    worker_root_write,
                    f"{path}\n".encode("utf-8"),
                )
            return path

        def record_guardian(parent_watch_read, worker_root):
            guardian_pid, worker_life_write = real_establish_guard(
                parent_watch_read,
                worker_root,
            )
            if worker_root.name.startswith("raw-generation-"):
                os.write(
                    guardian_pid_write,
                    f"{guardian_pid}\n".encode("ascii"),
                )
            return guardian_pid, worker_life_write

        def wait_for_controller_death(*args, **kwargs):
            os.write(
                worker_pid_write,
                f"{os.getpid()}\n".encode("ascii"),
            )
            time.sleep(5)
            os.write(completion_write, b"continued")
            return real_run_service(*args, **kwargs)

        reconciler.tempfile.mkdtemp = record_worker_root
        reconciler._establish_parent_death_guard = record_guardian  # noqa: SLF001
        reconciler.raw_sync.run_service = wait_for_controller_death
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    controller = context.Process(target=controller_entry)
    controller.start()
    os.close(worker_pid_write)
    os.close(guardian_pid_write)
    os.close(worker_root_write)
    os.close(completion_write)
    try:
        for descriptor in (
            worker_pid_read,
            guardian_pid_read,
            worker_root_read,
        ):
            readable, _writable, _errors = select.select(
                [descriptor],
                [],
                [],
                5,
            )
            assert readable == [descriptor]
        worker_pid = int(os.read(worker_pid_read, 128).decode("ascii").strip())
        guardian_pid = int(os.read(guardian_pid_read, 128).decode("ascii").strip())
        worker_root = Path(os.read(worker_root_read, 4096).decode("utf-8").strip())
        assert worker_root.is_dir()
        os.kill(controller.pid, signal.SIGKILL)
        controller.join(timeout=5)
        assert controller.exitcode == -signal.SIGKILL

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            live_processes = []
            for child_pid in (worker_pid, guardian_pid):
                try:
                    status = reconciler.psutil.Process(child_pid).status()
                except reconciler.psutil.NoSuchProcess:
                    continue
                if status != reconciler.psutil.STATUS_ZOMBIE:
                    live_processes.append(child_pid)
            if not live_processes and not worker_root.exists():
                break
            time.sleep(0.02)
        else:
            raise AssertionError("Raw worker, guardian, or private root survived controller death")

        readable, _writable, _errors = select.select(
            [completion_read],
            [],
            [],
            0.2,
        )
        if readable:
            assert os.read(completion_read, 128) == b""
    finally:
        if controller.is_alive():
            controller.kill()
            controller.join(timeout=5)
        os.close(worker_pid_read)
        os.close(guardian_pid_read)
        os.close(worker_root_read)
        os.close(completion_read)


def test_public_apply_raw_generation_timeout_is_bounded_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    monkeypatch.setattr(
        reconciler,
        "_RAW_GENERATION_WORKER_MAX_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        lambda *_args, **_kwargs: time.sleep(10),
    )

    started = time.monotonic()
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_generation_worker_timeout",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert time.monotonic() - started < 2
    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001


def test_public_apply_preserves_child_write_scope_evidence_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    real_run_service = reconciler.raw_sync.run_service
    outside_database = tmp_path.parent / f"{tmp_path.name}-generation-external.db"

    def attempt_foreign_database_write(*args, **kwargs):
        try:
            with _sqlite_transaction(outside_database) as connection:
                connection.execute("CREATE TABLE forbidden(id INTEGER)")
        except PermissionError:
            pass
        else:
            raise AssertionError("generation worker foreign write was not blocked")
        return real_run_service(*args, **kwargs)

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        attempt_foreign_database_write,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_generation_worker_write_scope_violation",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert not outside_database.exists()
    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    outer = json.loads(
        reconciler._migration_receipt_path(  # noqa: SLF001
            backup_dir,
            plan["plan_hash"],
        ).read_text(encoding="utf-8")
    )
    inner = json.loads((backup_dir / outer["inner_receipt_filename"]).read_text(encoding="utf-8"))
    assert inner["raw_only_boundary_ok"] is False
    assert inner["blocked_process_mutation_count"] == 1
    assert inner["raw_generation_worker_count"] == 1


def test_public_apply_preserves_grandchild_write_scope_evidence_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    real_run_service = reconciler.raw_sync.run_service
    outside_database = tmp_path.parent / f"{tmp_path.name}-generation-grandchild.db"

    def attempt_grandchild_foreign_database_write(*args, **kwargs):
        grandchild = os.fork()
        if grandchild == 0:
            try:
                try:
                    with _sqlite_transaction(outside_database) as connection:
                        connection.execute("CREATE TABLE forbidden(id INTEGER)")
                except PermissionError:
                    os._exit(0)
                os._exit(92)
            except BaseException:
                os._exit(91)
        _waited, status = os.waitpid(grandchild, 0)
        if os.waitstatus_to_exitcode(status) != 0:
            raise AssertionError("generation grandchild foreign write was not blocked")
        return real_run_service(*args, **kwargs)

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        attempt_grandchild_foreign_database_write,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_generation_worker_write_scope_violation",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert not outside_database.exists()
    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    outer = json.loads(
        reconciler._migration_receipt_path(  # noqa: SLF001
            backup_dir,
            plan["plan_hash"],
        ).read_text(encoding="utf-8")
    )
    inner = json.loads((backup_dir / outer["inner_receipt_filename"]).read_text(encoding="utf-8"))
    assert inner["raw_only_boundary_ok"] is False
    assert inner["blocked_process_mutation_count"] == 1
    assert inner["raw_generation_worker_count"] == 1


def test_public_apply_blocks_generation_worker_backup_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    real_run_service = reconciler.raw_sync.run_service
    forbidden_backup_write = backup_dir / "worker-owned.txt"

    def attempt_backup_write(*args, **kwargs):
        try:
            forbidden_backup_write.write_text("forbidden", encoding="utf-8")
        except PermissionError:
            pass
        else:
            raise AssertionError("generation worker backup write was not blocked")
        return real_run_service(*args, **kwargs)

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        attempt_backup_write,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_generation_worker_write_scope_violation",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert not forbidden_backup_write.exists()
    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001


def test_public_apply_blocks_generation_worker_snapshot_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    real_run_service = reconciler.raw_sync.run_service

    def attempt_snapshot_write(*args, **kwargs):
        snapshot_source = kwargs["source_registry"].list_sources()[0]
        snapshot_session = next(iter(snapshot_source._snapshot_sessions.values()))  # noqa: SLF001
        try:
            Path(snapshot_session.source_path).write_bytes(b"forbidden")
        except PermissionError:
            pass
        else:
            raise AssertionError("generation worker snapshot write was not blocked")
        return real_run_service(*args, **kwargs)

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        attempt_snapshot_write,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_generation_worker_write_scope_violation",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert source_path.read_text(encoding="utf-8") == "synthetic-safe"
    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001


def test_public_apply_rejects_inconsistent_raw_generation_worker_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    monkeypatch.setattr(
        reconciler,
        "_safe_cycle_report",
        lambda *_args, **_kwargs: {
            "errors": 0,
            "sources": {},
        },
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_generation_worker_report_invalid",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001


def test_public_apply_raw_generation_rss_and_report_budgets_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    real_run_service = reconciler.raw_sync.run_service
    backup_dir = tmp_path / "rss-backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    monkeypatch.setattr(
        reconciler,
        "_RAW_GENERATION_WORKER_MAX_RSS_BYTES",
        -1,
    )
    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        lambda *_args, **_kwargs: time.sleep(10),
    )
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_generation_worker_budget_exceeded",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    monkeypatch.setattr(
        reconciler,
        "_RAW_GENERATION_WORKER_MAX_RSS_BYTES",
        1024**3,
    )
    monkeypatch.setattr(
        reconciler,
        "_RAW_GENERATION_WORKER_MAX_REPORT_BYTES",
        1,
    )
    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        real_run_service,
    )
    backup_dir = tmp_path / "report-backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_generation_worker_report_budget_exceeded",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )


def test_public_apply_raw_generation_cleanup_failure_is_typed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    real_rmtree = reconciler.shutil.rmtree
    leaked_worker_roots: list[Path] = []

    def skip_raw_generation_cleanup(path, *args, **kwargs):
        candidate = Path(path)
        if candidate.name.startswith("raw-generation-"):
            leaked_worker_roots.append(candidate)
            return None
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        reconciler.shutil,
        "rmtree",
        skip_raw_generation_cleanup,
    )

    try:
        with pytest.raises(
            reconciler.AgentSourceRawReconciliationError,
            match="raw_generation_worker_cleanup_failed",
        ):
            reconciler.reconcile_active_source_raw_capture(
                config=config,
                raw_db_path=raw_path,
                backup_dir=backup_dir,
                sources=[source],
                apply=True,
                cycles=2,
                require_all_active_sources=False,
                runtime_writers_are_inactive=lambda: True,
                expected_plan_hash=plan["plan_hash"],
            )
    finally:
        for worker_root in set(leaked_worker_roots):
            real_rmtree(worker_root)


def test_public_apply_types_migration_lock_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)

    class _UnavailableLock:
        def __enter__(self):
            raise RuntimeError("injected migration lock failure")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        reconciler,
        "offline_migration_lock",
        lambda *_args, **_kwargs: _UnavailableLock(),
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="writer_lock_unavailable",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=tmp_path / "backups",
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash="sha256:" + ("a" * 64),
        )


def test_public_apply_rolls_back_on_post_apply_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001

    def interrupt_post_apply(**_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(reconciler, "_post_apply_raw_gap", interrupt_post_apply)

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="reconciliation_interrupted",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    inner_receipts = list(backup_dir.glob("agent-source-raw-reconciliation-2*.json"))
    assert len(inner_receipts) == 1
    marker = json.loads(inner_receipts[0].read_text())
    assert marker["status"] == ("rolled_back_by_migration_certification")
    invalidated = backup_dir / marker["invalidated_receipt_filename"]
    assert invalidated.is_file()
    assert reconciler._file_sha256(invalidated) == (  # noqa: SLF001
        marker["invalidated_receipt_sha256"].removeprefix("sha256:")
    )


def test_prepared_raw_recovery_rejects_code_runtime_drift_before_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001

    def killed_after_inner_apply():
        reconciler._post_apply_raw_gap = lambda **_kwargs: os._exit(77)  # noqa: SLF001
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    process = multiprocessing.get_context("fork").Process(target=killed_after_inner_apply)
    process.start()
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise AssertionError("fault-injection child did not terminate")
    assert process.exitcode == 77
    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        plan["plan_hash"],
    )
    prepared_outer = json.loads(receipt_path.read_text())
    assert prepared_outer["status"] == "prepared"
    assert len(prepared_outer["inner_prepared_receipt_sha256"]) == 64
    prepared_inner_path = backup_dir / prepared_outer["inner_receipt_filename"]
    completed_inner_bytes = prepared_inner_path.read_bytes()
    completed_inner = json.loads(completed_inner_bytes)
    assert completed_inner["prepared_receipt_sha256"] == (
        prepared_outer["inner_prepared_receipt_sha256"]
    )
    assert reconciler._target_state(config, raw_path) != before  # noqa: SLF001
    crashed_state = reconciler._target_state(config, raw_path)  # noqa: SLF001

    prepared_inner_path.write_bytes(completed_inner_bytes + b"\n")
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_binding_mismatch",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )
    prepared_inner_path.write_bytes(completed_inner_bytes)

    current_runtime = reconciler.runtime_execution_identity()
    with monkeypatch.context() as drift:
        drift.setattr(
            reconciler,
            "runtime_execution_identity",
            lambda: {**current_runtime, "python_version": "drifted"},
        )
        with pytest.raises(
            reconciler.AgentSourceRawReconciliationError,
            match="migration_receipt_code_drift",
        ):
            reconciler.reconcile_active_source_raw_capture(
                config=config,
                raw_db_path=raw_path,
                backup_dir=backup_dir,
                sources=[source],
                apply=True,
                cycles=2,
                require_all_active_sources=False,
                runtime_writers_are_inactive=lambda: True,
                expected_plan_hash=plan["plan_hash"],
            )
    assert reconciler._target_state(config, raw_path) == crashed_state  # noqa: SLF001
    assert json.loads(receipt_path.read_text())["status"] == "prepared"

    current_dependencies = reconciler._recovery_execution_dependency_hashes  # noqa: SLF001
    with monkeypatch.context() as drift:
        drift.setattr(
            reconciler,
            "_recovery_execution_dependency_hashes",
            lambda: {
                **current_dependencies(),
                "core/recovery_drift.py": "sha256:" + "0" * 64,
            },
        )
        with pytest.raises(
            reconciler.AgentSourceRawReconciliationError,
            match="migration_receipt_code_drift",
        ):
            reconciler.reconcile_active_source_raw_capture(
                config=config,
                raw_db_path=raw_path,
                backup_dir=backup_dir,
                sources=[source],
                apply=True,
                cycles=2,
                require_all_active_sources=False,
                runtime_writers_are_inactive=lambda: True,
                expected_plan_hash=plan["plan_hash"],
            )
    assert reconciler._target_state(config, raw_path) == crashed_state  # noqa: SLF001
    assert json.loads(receipt_path.read_text())["status"] == "prepared"

    resumed = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )

    assert resumed["ok"] is True
    assert resumed["second_apply_changed"] is False
    assert json.loads(receipt_path.read_text())["status"] == "completed"
    rolled_back_inner = [
        json.loads(path.read_text()).get("status")
        for path in backup_dir.glob("agent-source-raw-reconciliation-*.json")
    ]
    assert "rolled_back_by_migration_certification" in rolled_back_inner


def test_public_apply_recovers_after_process_exit_mid_rollback(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )

    def killed_during_rollback():
        def fail_certification(**_kwargs):
            raise reconciler.AgentSourceRawReconciliationError("forced_gap")

        def restore_only_first_target(
            *,
            backups,
            raw_db_path,
            cursor_path,
            coverage_path,
        ):
            del cursor_path, coverage_path
            reconciler._restore_sqlite_backup(  # noqa: SLF001
                backups["raw"][0],
                raw_db_path,
            )
            os._exit(78)

        reconciler._post_apply_raw_gap = fail_certification  # noqa: SLF001
        reconciler._restore_recovery_state = restore_only_first_target  # noqa: SLF001
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    process = multiprocessing.get_context("fork").Process(target=killed_during_rollback)
    process.start()
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise AssertionError("rollback fault-injection child did not terminate")
    assert process.exitcode == 78
    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        plan["plan_hash"],
    )
    assert json.loads(receipt_path.read_text())["status"] == "prepared"

    resumed = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )

    assert resumed["ok"] is True
    assert resumed["required_gap"] == 0
    assert json.loads(receipt_path.read_text())["status"] == "completed"


def test_prepared_raw_receipt_cannot_restore_attacker_controlled_backups(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    attacker = backup_dir / "attacker-raw.sqlite"
    with _sqlite_transaction(attacker) as connection:
        connection.execute("CREATE TABLE attacker_raw (value TEXT)")
        connection.execute("INSERT INTO attacker_raw VALUES ('forged')")
    attacker.chmod(0o600)
    backups = {
        "raw": {
            "present": True,
            "filename": attacker.name,
            "sha256": reconciler._file_sha256(attacker),  # noqa: SLF001
            "integrity": "ok",
        },
        "cursor": {"present": False, "integrity": "not_applicable", "sha256": ""},
        "coverage": {"present": False, "integrity": "not_applicable", "sha256": ""},
    }
    before_state = {
        "raw": plan["apply_scope"]["raw_db"],
        "cursor": plan["apply_scope"]["cursor_db"],
        "coverage": plan["apply_scope"]["coverage_state"],
    }
    inner_name = "agent-source-raw-reconciliation-forged.json"
    inner_receipt = backup_dir / inner_name
    inner_receipt.write_text(
        json.dumps(
            {
                "schema_version": reconciler.SCHEMA_VERSION,
                "status": "prepared",
                "reviewed_plan_hash": plan["plan_hash"],
                "support_manifest_hash": plan["support_manifest_hash"],
                "active_sources": plan["active_sources"],
                "backups": backups,
                "before_state": before_state,
            }
        ),
        encoding="utf-8",
    )
    inner_receipt.chmod(0o600)
    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        plan["plan_hash"],
    )
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": "mnemos.agent_source_raw_migration_receipt.v1",
                "status": "prepared",
                "plan_hash": plan["plan_hash"],
                "reviewed_plan": plan["canonical_plan"],
                "raw_db": str(raw_path.resolve()),
                "backup_dir": str(backup_dir.resolve()),
                "native_inventory_hash": plan["native_artifact_inventory"]["inventory_hash"],
                "before_state": before_state,
                "before_conservation": plan["raw_conservation"],
                "backups": backups,
                "inner_receipt_filename": inner_name,
                "inner_prepared_receipt_sha256": (
                    reconciler._file_sha256(inner_receipt)  # noqa: SLF001
                ),
            }
        ),
        encoding="utf-8",
    )
    receipt_path.chmod(0o600)
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_backup_invalid",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    with _sqlite_transaction(raw_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='attacker_raw'"
            ).fetchone()[0]
            == 0
        )


def test_same_plan_raw_receipt_rejects_missing_backup(tmp_path: Path):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    first = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )
    receipt = json.loads((backup_dir / first["receipt_filename"]).read_text())
    raw_backup = backup_dir / receipt["backups"]["raw"]["filename"]
    raw_backup.unlink()

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_backup_invalid",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )


def test_same_plan_raw_receipt_rejects_runtime_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )
    current = reconciler.runtime_execution_identity()
    monkeypatch.setattr(
        reconciler,
        "runtime_execution_identity",
        lambda: {**current, "python_version": "drifted"},
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_code_drift",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )


def test_same_plan_raw_receipt_rejects_public_backup_permissions(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    first = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )
    receipt = json.loads((backup_dir / first["receipt_filename"]).read_text())
    raw_backup = backup_dir / receipt["backups"]["raw"]["filename"]
    raw_backup.chmod(0o644)

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_backup_invalid",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )


def test_same_plan_raw_receipt_rejects_public_receipt_permissions(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    first = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )
    receipt = backup_dir / first["receipt_filename"]
    receipt.chmod(0o644)

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_permissions_invalid",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )


def test_same_plan_raw_receipt_detects_physical_scope_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )
    original = reconciler._post_apply_raw_gap  # noqa: SLF001

    def inject_scope_write(**kwargs):
        value = original(**kwargs)
        (backup_dir / "unexpected-second-apply-write").write_text("drift")
        return value

    monkeypatch.setattr(reconciler, "_post_apply_raw_gap", inject_scope_write)
    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_second_apply_physical_drift",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )


def test_raw_receipt_publish_fsyncs_parent_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "private" / "receipt.json"
    events: list[tuple[str, Path]] = []
    original_replace = reconciler.os.replace

    def tracked_replace(source, destination):
        original_replace(source, destination)
        events.append(("replace", Path(destination)))

    monkeypatch.setattr(reconciler.os, "replace", tracked_replace)
    monkeypatch.setattr(
        recovery_support,
        "fsync_directory",
        lambda path: events.append(("fsync", Path(path))),
    )
    reconciler._write_receipt(target, {"status": "prepared"})  # noqa: SLF001

    assert events[-2:] == [
        ("replace", target),
        ("fsync", target.parent),
    ]


def test_same_plan_raw_receipt_rejects_forged_before_comparator(tmp_path: Path):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    store.upsert_turn(
        source_agent="claude",
        session_id="existing",
        turn_number=0,
        user_content="existing-user",
        assistant_content="existing-assistant",
    )
    store.close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    first = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )
    receipt_path = backup_dir / first["receipt_filename"]
    receipt = json.loads(receipt_path.read_text())
    receipt["first_apply_comparator"]["before"]["raw_turns"]["row_set_hash"] = "sha256:" + "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_comparator_drift",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )


def test_raw_conservation_rejects_existing_row_and_retention_mutation(tmp_path: Path):
    config = _Config(tmp_path)
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    store.upsert_turn(
        source_agent="codex",
        session_id="existing",
        turn_number=0,
        user_content="existing-user",
        assistant_content="existing-assistant",
    )
    store.close()
    before = reconciler._safe_raw_conservation(raw_path)  # noqa: SLF001

    with _sqlite_transaction(raw_path) as connection:
        connection.execute("UPDATE raw_metrics SET retention_state='deleted'")

    retention_tampered = reconciler._safe_raw_conservation(raw_path)  # noqa: SLF001
    assert reconciler._compare_raw_conservation(before, retention_tampered) is False  # noqa: SLF001
    assert {
        (finding["table"], finding["rule"])
        for finding in reconciler._raw_conservation_findings(  # noqa: SLF001
            before,
            retention_tampered,
        )
    } >= {
        ("raw_metrics", "governed_metric_changed_without_observation"),
        ("raw_metrics", "invalid_retention_transition"),
    }

    with _sqlite_transaction(raw_path) as connection:
        connection.execute("UPDATE raw_metrics SET retention_state='active'")
        connection.execute("UPDATE raw_turns SET model_tag='tampered'")

    row_tampered = reconciler._safe_raw_conservation(raw_path)  # noqa: SLF001
    assert reconciler._compare_raw_conservation(before, row_tampered) is False  # noqa: SLF001
    assert {
        (finding["table"], finding["rule"])
        for finding in reconciler._raw_conservation_findings(  # noqa: SLF001
            before,
            row_tampered,
        )
    } >= {
        ("raw_turns", "current_revision_projection_invalid"),
        ("raw_turns", "stable_projection_changed_without_revision"),
    }

    with _sqlite_transaction(raw_path) as connection:
        connection.execute("UPDATE raw_turns SET model_tag=NULL")
        connection.execute("UPDATE raw_metrics SET confidence=0.123, survival_score=0.456")

    metrics_tampered = reconciler._safe_raw_conservation(raw_path)  # noqa: SLF001
    assert reconciler._compare_raw_conservation(before, metrics_tampered) is False  # noqa: SLF001


def test_raw_receipt_records_conservation_and_real_post_gap(tmp_path: Path):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )

    applied = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )
    receipt = json.loads((backup_dir / applied["receipt_filename"]).read_text())

    assert receipt["first_apply_comparator"]["conservation_ok"] is True
    assert receipt["first_apply_comparator"]["before"]["raw_turns"]["row_count"] == 0
    assert receipt["first_apply_comparator"]["after"]["raw_turns"]["row_count"] == 1
    assert receipt["post_apply_gap"]["required_gap"] == 0
    assert receipt["post_apply_gap"]["ok"] is True
    assert all(
        (backup_dir / item["filename"]).stat().st_mode & 0o777 == 0o600
        for item in receipt["backups"].values()
        if item["present"]
    )


def test_apply_reconciles_preexisting_current_projection_drift_losslessly(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    restore_revision_id = store.upsert_turn(
        source_agent="codex",
        session_id="legacy-restore",
        turn_number=1,
        user_content="restore-user",
        assistant_content="restore-assistant",
    )
    append_revision_id = store.upsert_turn(
        source_agent="codex",
        session_id="legacy-append",
        turn_number=2,
        user_content="old-user",
        assistant_content="append-assistant",
    )
    store.close()
    with _sqlite_transaction(raw_path) as connection:
        restore_event_id = str(
            connection.execute(
                "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
                (restore_revision_id,),
            ).fetchone()[0]
        )
        append_event_id = str(
            connection.execute(
                "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
                (append_revision_id,),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE raw_turns SET turn_number=999 WHERE event_id=?",
            (restore_event_id,),
        )
        replacement = "new-user-preserved"
        connection.execute(
            """
            UPDATE raw_turns
            SET user_content_blob=?, content_hash=?, full_content_hash=?,
                raw_bytes=raw_bytes + ?, updated_at='legacy-overwrite'
            WHERE event_id=?
            """,
            (
                zlib.compress(replacement.encode("utf-8")),
                "legacy-current-content-hash",
                "legacy-current-full-content-hash",
                len(replacement.encode("utf-8")) - len("old-user".encode("utf-8")),
                append_event_id,
            ),
        )

    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )

    assert plan["current_state_ok"] is False
    assert plan["ok"] is True
    assert plan["apply_eligible"] is True
    assert plan["current_projection_reconciliation"]["ok"] is True
    assert plan["current_projection_reconciliation"]["invalid_count"] == 2
    assert plan["current_projection_reconciliation"]["restore_revision_count"] == 1
    assert plan["current_projection_reconciliation"]["append_revision_count"] == 1

    applied = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )

    assert applied["ok"] is True
    assert applied["current_projection_reconciliation"] == {
        "append_revision_count": 1,
        "invalid_after_count": 0,
        "repaired_count": 2,
        "restore_revision_count": 1,
        "schema_version": "mnemos.raw_current_projection_reconciliation.v1",
    }
    migration_receipt = json.loads(
        (backup_dir / applied["receipt_filename"]).read_text(encoding="utf-8")
    )
    assert (
        migration_receipt["current_projection_reconciliation"]
        == applied["current_projection_reconciliation"]
    )
    with _sqlite_transaction(raw_path) as connection:
        assert (
            connection.execute(
                "SELECT turn_number FROM raw_turns WHERE event_id=?",
                (restore_event_id,),
            ).fetchone()[0]
            == 1
        )
        current_append_revision = str(
            connection.execute(
                "SELECT current_revision_id FROM raw_turns WHERE event_id=?",
                (append_event_id,),
            ).fetchone()[0]
        )
        assert current_append_revision != append_revision_id
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM raw_turn_revisions WHERE logical_event_id=?",
                (append_event_id,),
            ).fetchone()[0]
            == 2
        )
    repaired = RawEventStore(db_path=raw_path, config=config)
    assert repaired.get_turn(current_append_revision)["user_content"] == replacement
    repaired.close()
    current = reconciler._safe_raw_conservation(raw_path)  # noqa: SLF001
    assert reconciler._raw_conservation_findings(current, current) == []  # noqa: SLF001


def test_projection_restore_replays_same_observation_without_metric_churn(
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    revision_id = store.upsert_turn(
        source_agent="codex",
        session_id="observed-restore",
        turn_number=1,
        user_content="restore-user",
        assistant_content="restore-assistant",
    )
    store.close()
    observed_at = "2026-07-27T00:00:00+00:00"
    lifecycle_updated_at = "2026-07-27T01:00:00+00:00"
    with _sqlite_transaction(raw_path) as connection:
        event_id = str(
            connection.execute(
                """
                SELECT logical_event_id
                FROM raw_turn_revisions
                WHERE revision_id=?
                """,
                (revision_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO raw_native_contract_observations (
                observation_id, logical_event_id, observed_revision_id,
                support_manifest_hash, contract_state,
                contract_errors_json, observed_at
            ) VALUES (
                'observation-restore', ?, ?, 'manifest-hash',
                'conformant', '[]', ?
            )
            """,
            (event_id, revision_id, observed_at),
        )
        NativeRawContractLedger().refresh_effective_state(
            connection,
            logical_event_id=event_id,
            observed_at=observed_at,
        )
        connection.execute(
            "UPDATE raw_metrics SET updated_at=? WHERE event_id=?",
            (lifecycle_updated_at, event_id),
        )
        connection.execute(
            "UPDATE raw_turns SET turn_number=999 WHERE event_id=?",
            (event_id,),
        )
        metric_before = connection.execute(
            """
            SELECT confidence, survival_score, retention_state,
                   next_survival_recalc_at, updated_at
            FROM raw_metrics WHERE event_id=?
            """,
            (event_id,),
        ).fetchone()

    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=_Source(source_path),
    )
    applied = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=tmp_path / "backups",
        sources=[_Source(source_path)],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )

    assert applied["ok"] is True
    with _sqlite_transaction(raw_path) as connection:
        assert (
            connection.execute(
                """
                SELECT confidence, survival_score, retention_state,
                       next_survival_recalc_at, updated_at
                FROM raw_metrics WHERE event_id=?
                """,
                (event_id,),
            ).fetchone()
            == metric_before
        )


def test_projection_restore_does_not_excuse_semantic_metric_drift(
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    revision_id = store.upsert_turn(
        source_agent="codex",
        session_id="observed-semantic-drift",
        turn_number=1,
        user_content="restore-user",
        assistant_content="restore-assistant",
    )
    store.close()
    observed_at = "2026-07-27T00:00:00+00:00"
    with _sqlite_transaction(raw_path) as connection:
        event_id = str(
            connection.execute(
                """
                SELECT logical_event_id
                FROM raw_turn_revisions
                WHERE revision_id=?
                """,
                (revision_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO raw_native_contract_observations (
                observation_id, logical_event_id, observed_revision_id,
                support_manifest_hash, contract_state,
                contract_errors_json, observed_at
            ) VALUES (
                'observation-semantic-drift', ?, ?, 'manifest-hash',
                'conformant', '[]', ?
            )
            """,
            (event_id, revision_id, observed_at),
        )
        NativeRawContractLedger().refresh_effective_state(
            connection,
            logical_event_id=event_id,
            observed_at=observed_at,
        )
        connection.execute(
            """
            UPDATE raw_metrics
            SET confidence=0.123, updated_at='2026-07-27T01:00:00+00:00'
            WHERE event_id=?
            """,
            (event_id,),
        )
        connection.execute(
            "UPDATE raw_turns SET turn_number=999 WHERE event_id=?",
            (event_id,),
        )
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=_Source(source_path),
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="first_apply_conservation_failed",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[_Source(source_path)],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    receipt_path = reconciler._migration_receipt_path(  # noqa: SLF001
        backup_dir,
        plan["plan_hash"],
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "recovered_rollback"
    assert receipt["rollback_ok"] is True
    assert receipt["first_apply_comparator"]["conservation_findings"] == [
        {
            "table": "raw_metrics",
            "rule": "governed_metric_changed_without_observation",
            "mismatch_count": 1,
        }
    ]


def test_plan_blocks_unversioned_projection_with_contract_observation(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    revision_id = store.upsert_turn(
        source_agent="codex",
        session_id="observed-legacy",
        turn_number=1,
        user_content="old-user",
        assistant_content="old-assistant",
    )
    store.close()
    with _sqlite_transaction(raw_path) as connection:
        event_id = str(
            connection.execute(
                "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO raw_native_contract_observations (
                observation_id, logical_event_id, observed_revision_id,
                support_manifest_hash, contract_state,
                contract_errors_json, observed_at
            ) VALUES (
                'observation-1', ?, ?, 'manifest-hash',
                'conformant', '[]', '2026-07-27T00:00:00+00:00'
            )
            """,
            (event_id, revision_id),
        )
        connection.execute(
            """
            UPDATE raw_turns
            SET user_content_blob=?, content_hash=?,
                full_content_hash=?, updated_at='legacy-overwrite'
            WHERE event_id=?
            """,
            (
                zlib.compress(b"unversioned-user"),
                "unversioned-content-hash",
                "unversioned-full-content-hash",
                event_id,
            ),
        )

    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=_Source(source_path),
    )

    projection = plan["current_projection_reconciliation"]
    assert plan["current_state_ok"] is False
    assert plan["ok"] is False
    assert plan["apply_eligible"] is False
    assert projection["ok"] is False
    assert projection["invalid_count"] == 1
    assert projection["blocked_count"] == 1
    assert projection["blocked"][0]["reason"] == ("unversioned_projection_has_contract_observation")


@pytest.mark.parametrize(
    ("manifest_hash", "contract_state", "contract_errors_json"),
    (
        ("manifest-hash", "conformant", '["contradictory-error"]'),
        ("", "conformant", "[]"),
    ),
)
def test_current_projection_plan_blocks_malformed_latest_observation(
    tmp_path: Path,
    manifest_hash: str,
    contract_state: str,
    contract_errors_json: str,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    revision_id = store.upsert_turn(
        source_agent="codex",
        session_id="malformed-observation-plan",
        turn_number=1,
        user_content="user",
        assistant_content="assistant",
    )
    store.close()
    with _sqlite_transaction(raw_path) as connection:
        event_id = str(
            connection.execute(
                "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO raw_native_contract_observations (
                observation_id, logical_event_id, observed_revision_id,
                support_manifest_hash, contract_state,
                contract_errors_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "observation-malformed-plan",
                event_id,
                revision_id,
                manifest_hash,
                contract_state,
                contract_errors_json,
                "2026-07-30T00:00:00+00:00",
            ),
        )
        connection.execute(
            "UPDATE raw_turns SET turn_number=999 WHERE event_id=?",
            (event_id,),
        )

    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=_Source(source_path),
    )

    projection = plan["current_projection_reconciliation"]
    assert plan["apply_eligible"] is False
    assert projection["ok"] is False
    assert projection["append_revision_count"] == 0
    assert projection["blocked"] == [
        {
            "event_identity_hash": projection["blocked"][0]["event_identity_hash"],
            "reason": "latest_contract_observation_invalid",
        }
    ]


def test_current_projection_plan_blocks_cross_owner_pointer_without_appending_history(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    first_revision = store.upsert_turn(
        source_agent="codex",
        session_id="cross-owner-a",
        turn_number=1,
        user_content="first user",
        assistant_content="first assistant",
    )
    second_revision = store.upsert_turn(
        source_agent="codex",
        session_id="cross-owner-b",
        turn_number=1,
        user_content="second user",
        assistant_content="second assistant",
    )
    store.close()
    with _sqlite_transaction(raw_path) as connection:
        first_event = str(
            connection.execute(
                "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
                (first_revision,),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE raw_turns SET current_revision_id=? WHERE event_id=?",
            (second_revision, first_event),
        )

    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=_Source(source_path),
    )

    projection = plan["current_projection_reconciliation"]
    assert plan["apply_eligible"] is False
    assert projection["ok"] is False
    assert projection["append_revision_count"] == 0
    assert projection["blocked"][0]["reason"] == "current_revision_cross_owner"


def test_current_projection_plan_blocks_observation_bound_to_foreign_revision(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    current_revision = store.upsert_turn(
        source_agent="codex",
        session_id="foreign-observation-owner",
        turn_number=1,
        user_content="current user",
        assistant_content="assistant",
    )
    foreign_revision = store.upsert_turn(
        source_agent="codex",
        session_id="foreign-observation-revision",
        turn_number=1,
        user_content="foreign user",
        assistant_content="assistant",
    )
    store.close()
    with _sqlite_transaction(raw_path) as connection:
        event_id = str(
            connection.execute(
                "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
                (current_revision,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO raw_native_contract_observations (
                observation_id, logical_event_id, observed_revision_id,
                support_manifest_hash, contract_state,
                contract_errors_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "observation-foreign-revision",
                event_id,
                foreign_revision,
                "manifest-hash",
                "conformant",
                "[]",
                "2026-07-30T00:00:00+00:00",
            ),
        )

    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=_Source(source_path),
    )

    projection = plan["current_projection_reconciliation"]
    assert plan["apply_eligible"] is False
    assert projection["ok"] is False
    assert projection["blocked"][0]["reason"] == "latest_contract_observation_invalid"


def test_current_projection_plan_uses_one_read_snapshot_during_concurrent_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw_path = tmp_path / "raw_events.db"
    config = _Config(tmp_path)
    store = RawEventStore(db_path=raw_path, config=config)
    store.upsert_turn(
        source_agent="codex",
        session_id="snapshot-a",
        turn_number=1,
        user_content="first-user",
        assistant_content="first-assistant",
    )
    store.upsert_turn(
        source_agent="codex",
        session_id="snapshot-b",
        turn_number=2,
        user_content="second-user",
        assistant_content="second-assistant",
    )
    store.close()
    with _sqlite_transaction(raw_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        revision_ids = [str(row[0]) for row in connection.execute("""
                SELECT current_revision_id
                FROM raw_turns ORDER BY rowid
                """).fetchall()]
    original = raw_current_projection_reconciliation._revision_projection_matches
    committed = False

    def commit_between_projection_reads(*args, **kwargs):
        nonlocal committed
        result = original(*args, **kwargs)
        if not committed:
            committed = True
            with _sqlite_transaction(raw_path) as writer:
                writer.execute(
                    """
                    UPDATE raw_turn_revisions
                    SET content_hash='concurrent-revision-header'
                    WHERE revision_id=?
                    """,
                    (revision_ids[1],),
                )
                writer.commit()
        return result

    monkeypatch.setattr(
        raw_current_projection_reconciliation,
        "_revision_projection_matches",
        commit_between_projection_reads,
    )

    frozen = raw_current_projection_reconciliation.plan_current_projection_reconciliation(raw_path)
    monkeypatch.setattr(
        raw_current_projection_reconciliation,
        "_revision_projection_matches",
        original,
    )
    current = raw_current_projection_reconciliation.plan_current_projection_reconciliation(raw_path)

    assert committed is True
    assert frozen["invalid_count"] == 0
    assert frozen["action_fingerprints"] == []
    assert current["invalid_count"] == 1
    assert current["append_revision_count"] == 1


def test_exact_recovery_plan_binds_current_phase1_governance_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    binding = {
        "schema_version": "mnemos.phase1_recovery_governance_binding.v1",
        "ok": True,
        "record_id": "phase1-test-generation",
        "record_hash": "sha256:" + ("a" * 64),
        "execution_evidence_hash": "b" * 64,
        "post_deep_review_contract_hash": "sha256:" + ("c" * 64),
        "sequence_predecessor": "phase1-test-predecessor",
        "errors": [],
    }
    monkeypatch.setattr(
        reconciler,
        "_phase1_governance_generation_binding",
        lambda: binding,
    )
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()

    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=_Source(source_path),
    )

    assert plan["phase1_governance_generation"] == binding
    assert plan["canonical_plan"]["phase1_governance_generation"] == binding
    assert plan["apply_eligible"] is True


def test_phase1_governance_binding_requires_current_snapshot_and_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from scripts import phase1_governance_execution_validation as execution_validation

    ledger_path = tmp_path / "phase1-ledger.json"
    evidence_path = tmp_path / "phase1-evidence.json"
    data_path = tmp_path / "phase1-data.json"
    snapshot = {"path_count": 3, "sha256": "a" * 64}
    evidence_hash = "b" * 64
    record_id = "phase1-current-generation"
    predecessor_id = "phase1-predecessor-generation"
    record = {
        "root_id": "COG-045",
        "sequence_predecessor": predecessor_id,
        "supersedes_evidence_record": predecessor_id,
        "verification": {
            "phase1_execution_evidence_hash": evidence_hash,
        },
        "post_deep_review_contract": {
            "standards_review": "no_blocker",
            "spec_review": "no_blocker",
        },
        "governance_revalidation": {"status": "verified"},
        "artifacts": {"candidate": {"sha256": "c" * 64}},
        "closure_boundary": {
            "root_closed": False,
            "release_eligible": False,
            "production_effect": "not verified",
        },
    }
    ledger_path.write_text(
        json.dumps({predecessor_id: {}, record_id: record}),
        encoding="utf-8",
    )
    evidence_path.write_text(
        json.dumps(
            {
                "evidence_hash": evidence_hash,
                "candidate_snapshot": snapshot,
            }
        ),
        encoding="utf-8",
    )
    data_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(reconciler, "_PHASE1_LEDGER_PATH", ledger_path)
    monkeypatch.setattr(
        reconciler,
        "_PHASE1_EXECUTION_EVIDENCE_PATH",
        evidence_path,
    )
    monkeypatch.setattr(reconciler, "_PHASE1_GOVERNANCE_DATA_PATH", data_path)
    monkeypatch.setattr(
        reconciler,
        "PHASE1_REVALIDATION_SEQUENCE",
        (("COG-045", predecessor_id), ("COG-045", record_id)),
    )
    monkeypatch.setattr(
        execution_validation,
        "phase1_execution_snapshot",
        lambda: dict(snapshot),
    )

    current = _REAL_PHASE1_GOVERNANCE_BINDING()

    assert current["ok"] is True
    assert current["record_id"] == record_id
    assert current["errors"] == []

    evidence_path.write_text(
        json.dumps(
            {
                "evidence_hash": evidence_hash,
                "candidate_snapshot": {
                    "path_count": 3,
                    "sha256": "d" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    stale = _REAL_PHASE1_GOVERNANCE_BINDING()

    assert stale["ok"] is False
    assert "execution_evidence_candidate_snapshot_stale" in stale["errors"]


def test_same_plan_rejects_tampered_projection_reconciliation_receipt(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    revision_id = store.upsert_turn(
        source_agent="codex",
        session_id="legacy-restore",
        turn_number=1,
        user_content="restore-user",
        assistant_content="restore-assistant",
    )
    store.close()
    with _sqlite_transaction(raw_path) as connection:
        event_id = str(
            connection.execute(
                "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE raw_turns SET turn_number=999 WHERE event_id=?",
            (event_id,),
        )
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    applied = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )
    receipt_path = backup_dir / applied["receipt_filename"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["current_projection_reconciliation"]["repaired_count"] = 0
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="migration_receipt_binding_mismatch",
    ):
        reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=backup_dir,
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=plan["plan_hash"],
        )


def test_native_source_hash_binds_uncheckpointed_sqlite_wal(tmp_path: Path):
    native_path = tmp_path / "native-store-without-extension"
    connection = sqlite3.connect(native_path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY)")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source = _Source(native_path)
        before = build_native_artifact_inventory([source])

        connection.execute("INSERT INTO events VALUES ('event-1')")
        connection.commit()
        after = build_native_artifact_inventory([source])
    finally:
        connection.close()

    assert after.inventory_hash != before.inventory_hash


def test_public_apply_executes_final_raw_wal_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    backup_dir = tmp_path / "backups"
    plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=backup_dir,
        source=source,
    )
    real_connect = sqlite3.connect
    controller_pid = os.getpid()
    checkpoint_paths: list[Path] = []

    class _RecordingConnection:
        def __init__(self, connection, database) -> None:
            self._connection = connection
            self._database = database

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def execute(self, statement, *args, **kwargs):
            if str(statement).strip().upper() == "PRAGMA WAL_CHECKPOINT(TRUNCATE)":
                checkpoint_paths.append(Path(self._database).resolve())
            return self._connection.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def recording_connect(database, *args, **kwargs):
        if os.getpid() != controller_pid:
            return real_connect(database, *args, **kwargs)
        return _RecordingConnection(
            real_connect(database, *args, **kwargs),
            database,
        )

    monkeypatch.setattr(
        reconciler,
        "sqlite3",
        SimpleNamespace(
            connect=recording_connect,
            Error=sqlite3.Error,
        ),
    )

    result = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )

    assert result["ok"] is True
    assert checkpoint_paths == [raw_path.resolve()]


def test_resume_preserves_an_interrupted_generation_cursor_instead_of_restarting_it(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=config).close()
    source = _Source(source_path)
    first_plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=source,
    )
    reconciler._execute_unresolved_active_source_raw_capture_for_test(  # noqa: SLF001
        config=config,
        raw_db_path=raw_path,
        backup_dir=tmp_path / "backups",
        sources=[source],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=first_plan["plan_hash"],
    )
    before = AgentSyncCursorStore(tmp_path).get_session_raw_cursor("codex", "session")
    resume_plan = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=source,
        reset_derived_state=False,
    )

    resumed = reconciler._execute_unresolved_active_source_raw_capture_for_test(  # noqa: SLF001
        config=config,
        raw_db_path=raw_path,
        backup_dir=tmp_path / "backups",
        sources=[source],
        apply=True,
        cycles=2,
        reset_derived_state=False,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=resume_plan["plan_hash"],
    )

    after = AgentSyncCursorStore(tmp_path).get_session_raw_cursor("codex", "session")
    assert resumed["ok"] is True, resumed
    assert resumed["resets"] == []
    assert after.next_turn_number == before.next_turn_number == 1


def test_recovery_plan_budgets_full_roster_rotations_for_long_sessions(tmp_path: Path):
    class _LongSource(_Source):
        def discover_sessions(self):
            return [
                SessionInfo(session_id="short", source_path=self.path),
                SessionInfo(session_id="long", source_path=self.path.with_name("long.jsonl")),
            ]

        def parse_turns(self, path: Path):
            count = 205 if path.name == "long.jsonl" else 1
            return [
                Turn(
                    turn_number=index,
                    user_content="synthetic-user",
                    assistant_content="synthetic-assistant",
                    native_event_id=f"synthetic-native-{path.name}-{index}",
                )
                for index in range(count)
            ]

    path = tmp_path / "native.jsonl"
    path.write_text("synthetic-safe", encoding="utf-8")
    path.with_name("long.jsonl").write_text("synthetic-safe", encoding="utf-8")
    plan = reconciler._recovery_plan(  # noqa: SLF001 - verifies controlled bound
        [_LongSource(path)],
        batch_sessions=1,
        batch_turns=50,
        minimum_generations=2,
    )

    # Two source sessions form one 2-cycle roster rotation; the long session
    # needs five visits, so a nine/ten-turn denominator cannot be declared
    # complete after a single pass.
    assert plan["session_turn_batch_upper_bound"] == 5
    assert plan["generation_budget"] == 10


def test_reviewed_plan_reuses_challenger_shape_without_second_native_parse(
    tmp_path: Path,
):
    class _OnePassOnlySource(_Source):
        def __init__(self, path: Path, counter_write: int):
            super().__init__(path)
            self.counter_write = counter_write

        def discover_sessions(self):
            return [
                SessionInfo(session_id="short", source_path=self.path),
                SessionInfo(
                    session_id="long",
                    source_path=self.path.with_name("long.jsonl"),
                ),
            ]

        def parse_turns(self, path: Path):
            os.write(
                self.counter_write,
                f"{path.name}\n".encode("utf-8"),
            )
            turn_count = 205 if path.name == "long.jsonl" else 1
            return [
                Turn(
                    turn_number=index,
                    user_content="synthetic-user",
                    assistant_content="synthetic-assistant",
                    native_event_id=f"synthetic-{path.name}-{index}",
                )
                for index in range(turn_count)
            ]

    native = tmp_path / "native.jsonl"
    native.write_text("synthetic-safe", encoding="utf-8")
    native.with_name("long.jsonl").write_text(
        "synthetic-safe",
        encoding="utf-8",
    )
    counter_read, counter_write = os.pipe()
    raw_path = tmp_path / "raw_events.db"
    config = _Config(tmp_path)
    RawEventStore(db_path=raw_path, config=config).close()

    try:
        plan = reconciler.reconcile_active_source_raw_capture(
            config=config,
            raw_db_path=raw_path,
            backup_dir=tmp_path / "backups",
            sources=[_OnePassOnlySource(native, counter_write)],
            apply=False,
            cycles=2,
            batch_sessions=1,
            batch_turns=50,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
        )
    finally:
        os.close(counter_write)

    assert plan["apply_eligible"] is True
    assert plan["planning_limits"]["source_session_upper_bound"] == 2
    assert plan["planning_limits"]["session_turn_upper_bound"] == 205
    assert plan["planning_limits"]["session_turn_batch_upper_bound"] == 5
    assert plan["planning_limits"]["generation_budget"] == 10
    counts: dict[str, int] = {}
    for name in os.read(counter_read, 4096).decode("utf-8").splitlines():
        counts[name] = counts.get(name, 0) + 1
    os.close(counter_read)
    assert counts == {
        "long.jsonl": 1,
        "native.jsonl": 1,
    }


def test_reviewed_plan_rejects_challenger_native_parse_error(
    tmp_path: Path,
):
    class _BrokenSource(_Source):
        def parse_turns(self, _path: Path):
            raise RuntimeError("synthetic parser failure")

    native = tmp_path / "native.jsonl"
    native.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    config = _Config(tmp_path)
    RawEventStore(db_path=raw_path, config=config).close()

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_session_parse_failed",
    ):
        _reviewed_plan(
            config=config,
            raw_path=raw_path,
            backup_dir=tmp_path / "backups",
            source=_BrokenSource(native),
        )


@pytest.mark.parametrize(
    ("native_error", "expected_code"),
    [
        ("native_discovery_failed", "native_challenger_planning_evidence_invalid"),
        ("native_session_metadata_invalid", "native_challenger_planning_evidence_invalid"),
        ("native_session_id_missing", "native_challenger_planning_evidence_invalid"),
        ("native_canonical_session_duplicate", "native_challenger_planning_evidence_invalid"),
        (
            "native_session_parse_failed",
            "native_challenger_planning_evidence_invalid",
        ),
        ("native_turn_identity_invalid", "native_challenger_planning_evidence_invalid"),
        ("native_logical_identity_duplicate", "native_challenger_planning_evidence_invalid"),
    ],
)
def test_recovery_plan_rejects_every_challenger_native_error(
    native_error: str,
    expected_code: str,
):
    report = {
        "sources": {
            "codex": {
                "native_sessions": 1,
                "native_parsed_turns": 1,
                "native_session_turn_upper_bound": 1,
                "errors": [native_error],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match=expected_code,
    ):
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="codex")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )


def test_recovery_plan_preserves_validated_native_parse_failure_details():
    failure = {
        "attempt_count": 1,
        "error_code": "native_session_parser_exception",
        "exception_type": "NativeSourceContractError",
        "failure_class": "sqlite_nontransient",
        "reason_code": "native_opencode_artifact_evidence_failed",
        "session_id_hash": "sha256:" + ("a" * 64),
        "source_name": "opencode",
        "sqlite_errorcode": sqlite3.SQLITE_IOERR_GETTEMPPATH,
        "sqlite_errorname": "SQLITE_IOERR_GETTEMPPATH",
    }
    report = {
        "sources": {
            "opencode": {
                "native_sessions": 1,
                "native_parsed_turns": 0,
                "native_session_turn_upper_bound": 0,
                "errors": ["native_session_parse_failed"],
                "native_session_parse_failures": [failure],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_session_parse_failed",
    ) as raised:
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="opencode")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )

    assert raised.value.details == {
        "source_name": "opencode",
        "failures": [failure],
    }


def test_recovery_plan_accepts_exact_isolated_recovered_parse_evidence():
    recovery = {
        "attempt_count": 2,
        "error_code": "native_session_parser_retryable_exception",
        "exception_type": "NativeSourceContractError",
        "failure_class": "sqlite_transient",
        "reason_code": "native_cursor_artifact_evidence_failed",
        "session_id_hash": "sha256:" + ("b" * 64),
        "sqlite_errorcode": sqlite3.SQLITE_BUSY,
        "sqlite_errorname": "SQLITE_BUSY",
    }
    report = {
        "sources": {
            "cursor": {
                "native_sessions": 1,
                "native_parsed_turns": 1,
                "native_session_turn_upper_bound": 1,
                "native_identity_isolated_sessions": 1,
                "native_parse_recovered_sessions": 1,
                "native_parse_recovery_evidence": [recovery],
                "errors": [],
            }
        }
    }

    result = reconciler._recovery_plan(  # noqa: SLF001
        [SimpleNamespace(name="cursor")],
        batch_sessions=1,
        batch_turns=1,
        minimum_generations=2,
        challenger_report=report,
    )

    assert result["source_session_upper_bound"] == 1
    assert result["session_turn_upper_bound"] == 1


def test_recovery_plan_accepts_signal_recovered_by_snapshot_producer(
    tmp_path: Path,
) -> None:
    native = tmp_path / "session.jsonl"
    attempt_marker = tmp_path / "parser-attempted"
    native.write_text("native", encoding="utf-8")
    source = _Source(native)
    original_parse = source.parse_turns

    def signal_once(path: Path):
        if not attempt_marker.exists():
            attempt_marker.write_text("first", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGKILL)
        return original_parse(path)

    source.parse_turns = signal_once  # type: ignore[method-assign]
    with snapshot_native_sources([source]) as snapshot:
        snapshot_source = snapshot.sources[0]
        session = snapshot_source.discover_sessions()[0]
        parse_result = snapshot_source.parse_session_result(session)

    recovery = {
        "attempt_count": parse_result.infrastructure_attempt_count,
        "session_id_hash": "sha256:"
        + hashlib.sha256(session.session_id.lower().encode("utf-8")).hexdigest(),
        **parse_result.recovered_infrastructure_failure,
    }
    report = {
        "sources": {
            source.name: {
                "native_sessions": 1,
                "native_parsed_turns": len(parse_result.turns),
                "native_session_turn_upper_bound": len(parse_result.turns),
                "native_identity_isolated_sessions": 1,
                "native_parse_recovered_sessions": 1,
                "native_parse_recovery_evidence": [recovery],
                "errors": [],
            }
        }
    }

    result = reconciler._recovery_plan(  # noqa: SLF001
        [SimpleNamespace(name=source.name)],
        batch_sessions=1,
        batch_turns=1,
        minimum_generations=2,
        challenger_report=report,
    )

    assert recovery == {
        "attempt_count": 2,
        "error_code": "native_freeze_worker_signaled",
        "session_id_hash": recovery["session_id_hash"],
        "signal": signal.SIGKILL,
    }
    assert result["source_session_upper_bound"] == 1
    assert result["session_turn_upper_bound"] == 1


def test_recovery_plan_preserves_terminal_signal_from_real_challenger(
    tmp_path: Path,
) -> None:
    native = tmp_path / "session.jsonl"
    native.write_text("native", encoding="utf-8")
    source = _Source(native)

    def always_signal(_path: Path):
        os.kill(os.getpid(), signal.SIGKILL)

    source.parse_turns = always_signal  # type: ignore[method-assign]
    with snapshot_native_sources([source]) as snapshot:
        _expected, summary, errors = native_raw_challenger._expected_native_events(  # noqa: SLF001
            snapshot.sources[0]
        )
    report = {
        "sources": {
            source.name: {
                **summary,
                "errors": errors,
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_session_parse_failed",
    ) as raised:
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name=source.name)],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )

    assert raised.value.details == {
        "source_name": "codex",
        "failures": [
            {
                "attempt_count": 2,
                "error_code": "native_freeze_worker_signaled",
                "session_id_hash": ("sha256:" + hashlib.sha256(b"session").hexdigest()),
                "signal": signal.SIGKILL,
                "source_name": "codex",
            }
        ],
    }


@pytest.mark.parametrize(
    "error_code",
    (
        "native_freeze_worker_failed",
        "native_freeze_worker_setup_failed",
    ),
)
def test_recovery_plan_accepts_each_recovered_worker_failure(
    error_code: str,
) -> None:
    report = {
        "sources": {
            "codex": {
                "native_sessions": 1,
                "native_parsed_turns": 1,
                "native_session_turn_upper_bound": 1,
                "native_identity_isolated_sessions": 1,
                "native_parse_recovered_sessions": 1,
                "native_parse_recovery_evidence": [
                    {
                        "attempt_count": 2,
                        "error_code": error_code,
                        "session_id_hash": "sha256:" + ("a" * 64),
                    }
                ],
                "errors": [],
            }
        }
    }

    result = reconciler._recovery_plan(  # noqa: SLF001
        [SimpleNamespace(name="codex")],
        batch_sessions=1,
        batch_turns=1,
        minimum_generations=2,
        challenger_report=report,
    )

    assert result["source_session_upper_bound"] == 1
    assert result["session_turn_upper_bound"] == 1


@pytest.mark.parametrize(
    "recovery",
    (
        {
            "attempt_count": 2,
            "error_code": "native_freeze_worker_signaled",
            "session_id_hash": "sha256:" + ("a" * 64),
        },
        {
            "attempt_count": 2,
            "error_code": "native_freeze_worker_failed",
            "session_id_hash": "sha256:" + ("a" * 64),
            "signal": signal.SIGKILL,
        },
        {
            "attempt_count": 2,
            "error_code": "native_session_parser_retryable_exception",
            "session_id_hash": "sha256:" + ("a" * 64),
        },
        {
            "attempt_count": 2,
            "error_code": "native_unregistered_retry",
            "session_id_hash": "sha256:" + ("a" * 64),
        },
    ),
)
def test_recovery_plan_rejects_contradictory_or_unknown_recovery_evidence(
    recovery: dict[str, object],
) -> None:
    report = {
        "sources": {
            "codex": {
                "native_sessions": 1,
                "native_parsed_turns": 1,
                "native_session_turn_upper_bound": 1,
                "native_identity_isolated_sessions": 1,
                "native_parse_recovered_sessions": 1,
                "native_parse_recovery_evidence": [recovery],
                "errors": [],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_planning_evidence_invalid",
    ) as raised:
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="codex")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )

    assert raised.value.details == {
        "reason_code": "native_parse_recovery_evidence_invalid",
        "source_name": "codex",
    }


def test_recovery_plan_rejects_nonisolated_identity_denominator():
    report = {
        "sources": {
            "opencode": {
                "native_sessions": 1,
                "native_parsed_turns": 1,
                "native_session_turn_upper_bound": 1,
                "native_identity_isolated_sessions": 0,
                "native_parse_recovered_sessions": 0,
                "native_parse_recovery_evidence": [],
                "errors": [],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_planning_evidence_invalid",
    ):
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="opencode")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )


def test_recovery_plan_preserves_exact_native_error_reason() -> None:
    report = {
        "sources": {
            "codex": {
                "native_sessions": 0,
                "native_parsed_turns": 0,
                "native_session_turn_upper_bound": 0,
                "native_identity_isolated_sessions": 0,
                "native_parse_recovered_sessions": 0,
                "native_parse_recovery_evidence": [],
                "errors": ["native_discovery_failed"],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_planning_evidence_invalid",
    ) as raised:
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="codex")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )

    assert raised.value.details == {
        "reason_code": "native_discovery_failed",
        "source_name": "codex",
    }


def test_recovery_plan_reports_all_source_failures_in_one_pass() -> None:
    parse_failure = {
        "attempt_count": 1,
        "error_code": "native_freeze_worker_budget_exceeded",
        "session_id_hash": "sha256:" + ("a" * 64),
        "source_name": "codex",
    }
    report = {
        "sources": {
            "codex": {
                "native_sessions": 1,
                "native_parsed_turns": 0,
                "native_session_turn_upper_bound": 0,
                "errors": ["native_session_parse_failed"],
                "native_session_parse_failures": [parse_failure],
            },
            "opencode": {
                "native_sessions": 0,
                "native_parsed_turns": 0,
                "native_session_turn_upper_bound": 0,
                "errors": ["native_discovery_failed"],
            },
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_planning_evidence_invalid",
    ) as raised:
        reconciler._recovery_plan(  # noqa: SLF001
            [
                SimpleNamespace(name="codex"),
                SimpleNamespace(name="opencode"),
            ],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )

    assert raised.value.details == {
        "failure_count": 2,
        "failures": [
            parse_failure,
            {
                "error_code": ("native_challenger_planning_evidence_invalid"),
                "reason_code": "native_discovery_failed",
                "source_name": "opencode",
            },
        ],
        "source_failure_count": 2,
    }


def test_recovery_plan_reports_all_failures_within_one_source() -> None:
    parse_failure = {
        "attempt_count": 1,
        "error_code": "native_freeze_worker_budget_exceeded",
        "session_id_hash": "sha256:" + ("a" * 64),
        "source_name": "codex",
    }
    report = {
        "sources": {
            "codex": {
                "native_sessions": 2,
                "native_parsed_turns": 0,
                "native_session_turn_upper_bound": 0,
                "errors": [
                    "native_session_parse_failed",
                    "native_turn_identity_invalid",
                ],
                "native_session_parse_failures": [parse_failure],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_planning_evidence_invalid",
    ) as raised:
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="codex")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )

    assert raised.value.details == {
        "failure_count": 2,
        "failures": [
            parse_failure,
            {
                "error_code": ("native_challenger_planning_evidence_invalid"),
                "reason_code": "native_turn_identity_invalid",
                "source_name": "codex",
            },
        ],
        "source_failure_count": 1,
    }


def test_recovery_plan_flattens_all_cross_source_failure_groups() -> None:
    parse_failure = {
        "attempt_count": 1,
        "error_code": "native_freeze_worker_budget_exceeded",
        "session_id_hash": "sha256:" + ("a" * 64),
        "source_name": "codex",
    }
    report = {
        "sources": {
            "codex": {
                "native_sessions": 2,
                "native_parsed_turns": 0,
                "native_session_turn_upper_bound": 0,
                "errors": [
                    "native_session_parse_failed",
                    "native_turn_identity_invalid",
                ],
                "native_session_parse_failures": [parse_failure],
            },
            "opencode": {
                "native_sessions": 0,
                "native_parsed_turns": 0,
                "native_session_turn_upper_bound": 0,
                "errors": ["native_discovery_failed"],
            },
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_planning_evidence_invalid",
    ) as raised:
        reconciler._recovery_plan(  # noqa: SLF001
            [
                SimpleNamespace(name="codex"),
                SimpleNamespace(name="opencode"),
            ],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )

    assert raised.value.details == {
        "failure_count": 3,
        "failures": [
            parse_failure,
            {
                "error_code": ("native_challenger_planning_evidence_invalid"),
                "reason_code": "native_turn_identity_invalid",
                "source_name": "codex",
            },
            {
                "error_code": ("native_challenger_planning_evidence_invalid"),
                "reason_code": "native_discovery_failed",
                "source_name": "opencode",
            },
        ],
        "source_failure_count": 2,
    }


def test_recovery_plan_reports_all_shape_failures_within_one_source() -> None:
    report = {
        "sources": {
            "codex": {
                "native_sessions": 1,
                "native_parsed_turns": 2,
                "native_session_turn_upper_bound": 3,
                "native_identity_isolated_sessions": 0,
                "native_parse_recovered_sessions": 1,
                "native_parse_recovery_evidence": [],
                "errors": [],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_planning_evidence_invalid",
    ) as raised:
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="codex")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )

    assert raised.value.details == {
        "failure_count": 3,
        "failures": [
            {
                "error_code": ("native_challenger_planning_evidence_invalid"),
                "reason_code": "native_session_turn_upper_bound_invalid",
                "source_name": "codex",
            },
            {
                "error_code": ("native_challenger_planning_evidence_invalid"),
                "reason_code": ("native_identity_isolation_count_mismatch"),
                "source_name": "codex",
            },
            {
                "error_code": ("native_challenger_planning_evidence_invalid"),
                "reason_code": "native_parse_recovery_count_mismatch",
                "source_name": "codex",
            },
        ],
        "source_failure_count": 1,
    }


def test_recovery_plan_rejects_unvalidated_native_parse_failure_details():
    report = {
        "sources": {
            "codex": {
                "native_sessions": 1,
                "native_parsed_turns": 0,
                "native_session_turn_upper_bound": 0,
                "errors": ["native_session_parse_failed"],
                "native_session_parse_failures": [
                    {
                        "attempt_count": 2,
                        "error_code": "native_freeze_worker_signaled",
                        "session_id_hash": "not-a-hash",
                        "signal": signal.SIGKILL,
                        "source_name": "codex",
                        "unexpected": "must-fail-closed",
                    }
                ],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_planning_evidence_invalid",
    ):
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="codex")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )


def test_recovery_plan_preserves_worker_budget_failure_evidence():
    failure = {
        "attempt_count": 1,
        "error_code": "native_freeze_worker_budget_exceeded",
        "session_id_hash": "sha256:" + ("a" * 64),
        "source_name": "codex",
    }
    report = {
        "sources": {
            "codex": {
                "native_sessions": 1,
                "native_parsed_turns": 0,
                "native_session_turn_upper_bound": 0,
                "errors": ["native_session_parse_failed"],
                "native_session_parse_failures": [failure],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_session_parse_failed",
    ) as raised:
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="codex")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )

    assert raised.value.details == {
        "source_name": "codex",
        "failures": [failure],
    }


@pytest.mark.parametrize(
    ("error_code", "attempt_count", "extra"),
    (
        ("native_artifact_drift_during_freeze", 1, {}),
        ("native_freeze_budget_exceeded", 1, {}),
        ("native_freeze_worker_budget_exceeded", 1, {}),
        ("native_freeze_worker_failed", 2, {}),
        ("native_freeze_worker_setup_failed", 2, {}),
        (
            "native_freeze_worker_signaled",
            2,
            {"signal": signal.SIGKILL},
        ),
        ("native_freeze_worker_unavailable", 1, {}),
        (
            "native_parse_recovery_evidence_invalid",
            1,
            {"reason_code": "native_freeze_worker_signaled"},
        ),
        (
            "native_parse_terminal_error_unregistered",
            1,
            {"reason_code": "native_future_unregistered_parse_failure"},
        ),
        ("native_session_parser_exception", 1, {}),
        (
            "native_session_parser_retryable_exception",
            2,
            {"failure_class": "sqlite_transient"},
        ),
        ("native_snapshot_registry_unavailable", 1, {}),
        ("snapshot_session_identity_missing", 1, {}),
    ),
)
def test_recovery_plan_preserves_each_registered_terminal_parse_failure(
    error_code: str,
    attempt_count: int,
    extra: dict[str, object],
) -> None:
    failure = {
        "attempt_count": attempt_count,
        "error_code": error_code,
        "session_id_hash": "sha256:" + ("a" * 64),
        "source_name": "codex",
        **extra,
    }
    report = {
        "sources": {
            "codex": {
                "native_sessions": 1,
                "native_parsed_turns": 0,
                "native_session_turn_upper_bound": 0,
                "errors": ["native_session_parse_failed"],
                "native_session_parse_failures": [failure],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_session_parse_failed",
    ) as raised:
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="codex")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )

    assert raised.value.details == {
        "source_name": "codex",
        "failures": [failure],
    }


def test_snapshot_terminal_parse_code_contract_is_exact() -> None:
    assert SNAPSHOT_PARSE_TERMINAL_ERROR_CODES == frozenset(
        {
            "native_artifact_drift_during_freeze",
            "native_freeze_budget_exceeded",
            "native_freeze_worker_budget_exceeded",
            "native_freeze_worker_failed",
            "native_freeze_worker_setup_failed",
            "native_freeze_worker_signaled",
            "native_freeze_worker_unavailable",
            "native_parse_recovery_evidence_invalid",
            "native_parse_terminal_error_unregistered",
            "native_session_parser_exception",
            "native_session_parser_retryable_exception",
            "native_snapshot_registry_unavailable",
            "snapshot_session_identity_missing",
        }
    )


def test_recovery_plan_rejects_unregistered_terminal_parse_code() -> None:
    report = {
        "sources": {
            "codex": {
                "native_sessions": 1,
                "native_parsed_turns": 0,
                "native_session_turn_upper_bound": 0,
                "errors": ["native_session_parse_failed"],
                "native_session_parse_failures": [
                    {
                        "attempt_count": 1,
                        "error_code": "native_unknown_terminal_failure",
                        "session_id_hash": "sha256:" + ("a" * 64),
                        "source_name": "codex",
                    }
                ],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_planning_evidence_invalid",
    ) as raised:
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="codex")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )

    assert raised.value.details == {
        "reason_code": "native_parse_failure_evidence_invalid",
        "source_name": "codex",
    }


@pytest.mark.parametrize(
    "failure",
    (
        {
            "attempt_count": 2,
            "error_code": "native_freeze_worker_signaled",
            "session_id_hash": "sha256:" + ("a" * 64),
            "source_name": "codex",
        },
        {
            "attempt_count": 2,
            "error_code": "native_freeze_worker_failed",
            "session_id_hash": "sha256:" + ("a" * 64),
            "signal": signal.SIGKILL,
            "source_name": "codex",
        },
        {
            "attempt_count": 1,
            "error_code": "native_freeze_worker_failed",
            "session_id_hash": "sha256:" + ("a" * 64),
            "source_name": "codex",
        },
        {
            "attempt_count": 2,
            "error_code": "native_session_parser_retryable_exception",
            "session_id_hash": "sha256:" + ("a" * 64),
            "source_name": "codex",
        },
        {
            "attempt_count": 1,
            "error_code": "native_parse_recovery_evidence_invalid",
            "session_id_hash": "sha256:" + ("a" * 64),
            "source_name": "codex",
        },
        {
            "attempt_count": 1,
            "error_code": "native_parse_terminal_error_unregistered",
            "session_id_hash": "sha256:" + ("a" * 64),
            "source_name": "codex",
        },
    ),
)
def test_recovery_plan_rejects_contradictory_terminal_parse_failure_evidence(
    failure: dict[str, object],
) -> None:
    report = {
        "sources": {
            "codex": {
                "native_sessions": 1,
                "native_parsed_turns": 0,
                "native_session_turn_upper_bound": 0,
                "errors": ["native_session_parse_failed"],
                "native_session_parse_failures": [failure],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_planning_evidence_invalid",
    ) as raised:
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="codex")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )

    assert raised.value.details == {
        "reason_code": "native_parse_failure_evidence_invalid",
        "source_name": "codex",
    }


def test_cli_preserves_typed_native_parse_failure_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    failure = {
        "attempt_count": 2,
        "error_code": "native_freeze_worker_signaled",
        "session_id_hash": "sha256:" + ("a" * 64),
        "signal": signal.SIGKILL,
        "source_name": "codex",
    }
    monkeypatch.setattr(reconciler, "Config", lambda **_kwargs: _Config(tmp_path))
    monkeypatch.setattr(
        reconciler,
        "load_manifest_active_sources",
        lambda: [SimpleNamespace(name="codex")],
    )

    def fail_with_typed_details(**_kwargs):
        raise reconciler.AgentSourceRawReconciliationError(
            "native_session_parse_failed",
            details={"source_name": "codex", "failures": [failure]},
        )

    monkeypatch.setattr(
        reconciler,
        "reconcile_active_source_raw_capture",
        fail_with_typed_details,
    )

    exit_code = reconciler.main(["--confirm-read-native-history", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["error_code"] == "native_session_parse_failed"
    assert payload["error_details"] == {
        "source_name": "codex",
        "failures": [failure],
    }


def test_cli_summary_preserves_bounded_content_free_parse_failure_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    failure = {
        "attempt_count": 1,
        "error_code": "native_session_parser_exception",
        "exception_type": "NativeSourceContractError",
        "reason_code": "native_opencode_artifact_evidence_failed",
        "session_id_hash": "sha256:" + ("b" * 64),
        "source_name": "opencode",
        "sensitive_path": "/private/project/must-not-escape",
    }
    monkeypatch.setattr(reconciler, "Config", lambda **_kwargs: _Config(tmp_path))
    monkeypatch.setattr(
        reconciler,
        "load_manifest_active_sources",
        lambda: [SimpleNamespace(name="opencode")],
    )

    def fail_with_typed_details(**_kwargs):
        raise reconciler.AgentSourceRawReconciliationError(
            "native_session_parse_failed",
            details={"source_name": "opencode", "failures": [failure]},
        )

    monkeypatch.setattr(
        reconciler,
        "reconcile_active_source_raw_capture",
        fail_with_typed_details,
    )

    exit_code = reconciler.main(["--confirm-read-native-history", "--summary-json"])
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)

    assert exit_code == 1
    assert payload["schema_version"] == "mnemos.agent_source_raw_cli_summary.v2"
    assert payload["error_code"] == "native_session_parse_failed"
    assert payload["error_details"] == {
        "source_name": "opencode",
        "failure_count": 1,
        "failures": [{key: value for key, value in failure.items() if key != "sensitive_path"}],
        "failures_truncated": False,
    }
    assert "must-not-escape" not in rendered


def test_cli_summary_preserves_native_planning_failure_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(reconciler, "Config", lambda **_kwargs: _Config(tmp_path))
    monkeypatch.setattr(
        reconciler,
        "load_manifest_active_sources",
        lambda: [SimpleNamespace(name="codex")],
    )

    def fail_with_planning_reason(**_kwargs):
        raise reconciler.AgentSourceRawReconciliationError(
            "native_challenger_planning_evidence_invalid",
            details={
                "reason_code": "native_parse_failure_evidence_invalid",
                "source_name": "codex",
            },
        )

    monkeypatch.setattr(
        reconciler,
        "reconcile_active_source_raw_capture",
        fail_with_planning_reason,
    )

    exit_code = reconciler.main(["--confirm-read-native-history", "--summary-json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["error_code"] == ("native_challenger_planning_evidence_invalid")
    assert payload["error_details"] == {
        "reason_code": "native_parse_failure_evidence_invalid",
        "source_name": "codex",
    }


def test_cli_summary_preserves_cross_source_failure_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failures = [
        {
            "error_code": "native_freeze_worker_budget_exceeded",
            "source_name": "codex",
        },
        {
            "error_code": "native_challenger_planning_evidence_invalid",
            "reason_code": "native_discovery_failed",
            "source_name": "opencode",
        },
    ]
    monkeypatch.setattr(reconciler, "Config", lambda **_kwargs: _Config(tmp_path))
    monkeypatch.setattr(
        reconciler,
        "load_manifest_active_sources",
        lambda: [
            SimpleNamespace(name="codex"),
            SimpleNamespace(name="opencode"),
        ],
    )

    def fail_with_all_source_findings(**_kwargs):
        raise reconciler.AgentSourceRawReconciliationError(
            "native_challenger_planning_evidence_invalid",
            details={
                "failure_count": 2,
                "failures": failures,
                "source_failure_count": 2,
            },
        )

    monkeypatch.setattr(
        reconciler,
        "reconcile_active_source_raw_capture",
        fail_with_all_source_findings,
    )

    exit_code = reconciler.main(["--confirm-read-native-history", "--summary-json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["error_details"] == {
        "failure_count": 2,
        "failures": failures,
        "failures_truncated": False,
        "source_failure_count": 2,
    }


@pytest.mark.parametrize("apply", [False, True])
def test_public_recovery_preserves_snapshot_parse_failure_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    apply: bool,
):
    details = {
        "attempt_count": 2,
        "signal": signal.SIGKILL,
    }

    class FailingSnapshotContext:
        def __enter__(self):
            raise NativeArtifactInventoryError(
                "native_freeze_worker_signaled",
                details=details,
            )

        def __exit__(self, *_args):
            return False

    def fail_snapshot(_sources):
        return FailingSnapshotContext()

    @contextmanager
    def allow_offline_lock(*_args, **_kwargs):
        yield

    monkeypatch.setattr(
        reconciler,
        "snapshot_native_sources",
        fail_snapshot,
    )
    monkeypatch.setattr(
        reconciler,
        "offline_migration_lock",
        allow_offline_lock,
    )

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_freeze_worker_signaled",
    ) as raised:
        reconciler._execute_unresolved_active_source_raw_capture_for_test(  # noqa: SLF001
            config=_Config(tmp_path),
            raw_db_path=tmp_path / "raw_events.db",
            backup_dir=tmp_path / "backups",
            sources=[],
            apply=apply,
            runtime_writers_are_inactive=lambda: True,
            expected_plan_hash=("sha256:" + ("a" * 64)) if apply else "",
        )

    assert raised.value.details == details


@pytest.mark.parametrize(
    ("session_count", "parsed_turns", "session_turn_upper_bound"),
    [
        (1, 100, 1),
        (1, 0, 1),
        (1, 1, 0),
        (0, 1, 1),
    ],
)
def test_recovery_plan_rejects_impossible_challenger_shape(
    session_count: int,
    parsed_turns: int,
    session_turn_upper_bound: int,
):
    report = {
        "sources": {
            "codex": {
                "native_sessions": session_count,
                "native_parsed_turns": parsed_turns,
                "native_session_turn_upper_bound": session_turn_upper_bound,
                "native_identity_isolated_sessions": session_count,
                "native_parse_recovered_sessions": 0,
                "native_parse_recovery_evidence": [],
                "errors": [],
            }
        }
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="native_challenger_planning_evidence_invalid",
    ):
        reconciler._recovery_plan(  # noqa: SLF001
            [SimpleNamespace(name="codex")],
            batch_sessions=1,
            batch_turns=1,
            minimum_generations=2,
            challenger_report=report,
        )


def test_session_identity_plan_is_exact_and_unlocks_raw_replay(
    tmp_path: Path,
) -> None:
    native = tmp_path / "native.jsonl"
    native.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    config = _Config(tmp_path)
    store = RawEventStore(db_path=raw_path, config=config)
    store.upsert_turn(
        source_agent="openclaw",
        session_id="legacy-session",
        turn_number=0,
        user_content="legacy",
        assistant_content="payload",
    )
    store.close()
    source = _IdentityUpgradeSource(native)

    plan = reconciler._session_identity_reconciliation_plan(  # noqa: SLF001
        raw_path,
        [source],
    )

    assert plan["ok"] is True
    assert plan["required_receipt_count"] == 1
    applied = reconciler._apply_session_identity_reconciliation_plan(  # noqa: SLF001
        raw_path,
        plan=plan,
        reviewed_plan_hash="sha256:" + ("c" * 64),
    )
    assert applied["ok"] is True
    assert applied["recorded_receipt_count"] == 1
    assert reconciler._verify_session_identity_reconciliation_plan(  # noqa: SLF001
        raw_path,
        plan,
    )

    store = RawEventStore(db_path=raw_path, config=config)
    turn = source.parse_turns(native)[0]
    session = source.discover_sessions()[0]
    from daemon.raw_only_sync_engine import RawOnlySyncEngine

    result = RawOnlySyncEngine(raw_store=store).sync_turns(
        source,
        session,
        [turn],
        incremental=False,
        enqueue_distillation=False,
    )
    assert result[0].raw_event_id
    store.close()


def test_session_identity_apply_redacts_untyped_storage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = tmp_path / "native.jsonl"
    native.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    config = _Config(tmp_path)
    store = RawEventStore(db_path=raw_path, config=config)
    store.upsert_turn(
        source_agent="openclaw",
        session_id="legacy-session",
        turn_number=0,
        user_content="legacy",
        assistant_content="payload",
    )
    store.close()
    plan = reconciler._session_identity_reconciliation_plan(  # noqa: SLF001
        raw_path,
        [_IdentityUpgradeSource(native)],
    )

    def fail_connect(_path):
        raise sqlite3.OperationalError("sensitive database path and storage detail")

    monkeypatch.setattr(reconciler.sqlite3, "connect", fail_connect)

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="session_identity_reconciliation_apply_failed",
    ) as raised:
        reconciler._apply_session_identity_reconciliation_plan(  # noqa: SLF001
            raw_path,
            plan=plan,
            reviewed_plan_hash="sha256:" + ("c" * 64),
        )

    assert str(raised.value) == "session_identity_reconciliation_apply_failed"
    assert "sensitive" not in str(raised.value)


def test_session_identity_receipt_unlocks_standard_sync_engine_exact_context(
    tmp_path: Path,
) -> None:
    native = tmp_path / "native.jsonl"
    native.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    config = _Config(tmp_path)
    store = RawEventStore(db_path=raw_path, config=config)
    store.upsert_turn(
        source_agent="openclaw",
        session_id="legacy-session",
        turn_number=0,
        user_content="legacy",
        assistant_content="payload",
    )
    store.close()
    source = _IdentityUpgradeSource(native)
    plan = reconciler._session_identity_reconciliation_plan(  # noqa: SLF001
        raw_path,
        [source],
    )
    reconciler._apply_session_identity_reconciliation_plan(  # noqa: SLF001
        raw_path,
        plan=plan,
        reviewed_plan_hash="sha256:" + ("d" * 64),
    )
    store = RawEventStore(db_path=raw_path, config=config)
    backend = Mock()
    backend._sanitize = lambda value: value
    backend.list_by_tags.return_value = []
    backend.save.return_value = [SimpleNamespace(uid="sync-result")]
    engine = SyncEngine(
        backend=backend,
        db_path=str(tmp_path / "sync_log.db"),
        config=config,
        raw_store=store,
    )
    session = source.discover_sessions()[0]

    results = engine.sync_session(source, session, incremental=False)

    assert len(results) == 1
    assert results[0].action != "failed"
    assert results[0].raw_event_id
    wrong_artifact = source.discover_sessions()[0]
    wrong_artifact.metadata["source_artifact_id"] = "artifact-wrong"
    with pytest.raises(
        CanonicalRawCommitError,
        match="source_session_identity_reconciliation_required",
    ):
        engine.sync_session(source, wrong_artifact, incremental=False)
    missing_artifact = source.discover_sessions()[0]
    missing_artifact.metadata.pop("source_artifact_id")
    with pytest.raises(
        CanonicalRawCommitError,
        match="source_session_identity_reconciliation_required",
    ):
        engine.sync_session(source, missing_artifact, incremental=False)
    engine.close()


def test_session_identity_plan_rejects_historical_preimage_drift(
    tmp_path: Path,
) -> None:
    native = tmp_path / "native.jsonl"
    native.write_text("synthetic-safe", encoding="utf-8")
    raw_path = tmp_path / "raw_events.db"
    config = _Config(tmp_path)
    store = RawEventStore(db_path=raw_path, config=config)
    store.upsert_turn(
        source_agent="openclaw",
        session_id="legacy-session",
        turn_number=0,
        user_content="legacy-0",
        assistant_content="payload",
    )
    store.close()
    source = _IdentityUpgradeSource(native)
    plan = reconciler._session_identity_reconciliation_plan(  # noqa: SLF001
        raw_path,
        [source],
    )

    store = RawEventStore(db_path=raw_path, config=config)
    store.upsert_turn(
        source_agent="openclaw",
        session_id="legacy-session",
        turn_number=1,
        user_content="legacy-1",
        assistant_content="payload",
    )
    store.close()

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="preimage_drift",
    ):
        reconciler._apply_session_identity_reconciliation_plan(  # noqa: SLF001
            raw_path,
            plan=plan,
            reviewed_plan_hash="sha256:" + ("d" * 64),
        )


def test_nonretryable_source_identity_failure_stops_after_first_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native = tmp_path / "native.jsonl"
    native.write_text("synthetic-safe", encoding="utf-8")
    source = _Source(native)
    config = _Config(tmp_path)
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    store.close()
    before = reconciler._target_state(config, raw_path)  # noqa: SLF001
    reviewed = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=source,
    )

    def fail_once(log_service_error, **_kwargs):
        error = RawEventIdentitySchemaMigrationRequired(
            "source_session_identity_reconciliation_required"
        )
        log_service_error("raw_sync:codex", error)
        return {
            "errors": 1,
            "source_coverage": {
                "sources": {
                    "codex": {
                        "status": "error",
                        "gap": "source_error",
                        "native_sessions": 1,
                        "native_turns": 1,
                        "cursor": {},
                    }
                }
            },
            "source_snapshots": {},
        }

    monkeypatch.setattr(reconciler.raw_sync, "run_service", fail_once)
    identity_plan = {
        "schema_version": "mnemos.raw_session_identity_reconciliation_plan.v1",
        "mode": "append_only_exact_historical_event_set_approval",
        "required_receipt_count": 0,
        "receipt_material_hash": reconciler._canonical_hash([]),  # noqa: SLF001
        "receipts": [],
        "unresolved": [],
        "ok": True,
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_reconciliation_nonretryable_source_failure",
    ):
        reconciler._reconcile_active_source_raw_capture_unlocked(  # noqa: SLF001
            config=config,
            raw_db_path=raw_path,
            backup_dir=tmp_path / "backups",
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            session_identity_reconciliation=identity_plan,
            reviewed_before_challenger=reviewed["before_challenger"],
            reviewed_plan_hash="sha256:" + ("f" * 64),
        )

    assert reconciler._target_state(config, raw_path) == before  # noqa: SLF001
    receipts = list((tmp_path / "backups").glob("agent-source-raw-reconciliation-*.json"))
    assert len(receipts) == 1
    evidence = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["rollback_ok"] is True
    assert evidence["terminal_failure_codes"] == ["source_session_identity_reconciliation_required"]
    assert evidence["cycles"][0]["error_evidence"][0]["error_type"] == (
        "RawEventIdentitySchemaMigrationRequired"
    )


@pytest.mark.parametrize(
    "error_code",
    [
        "native_freeze_worker_budget_exceeded",
        "native_session_parse_failed",
        "native_session_artifact_changed_during_parse",
        "native_canonical_session_duplicate",
    ],
)
def test_deterministic_native_failures_are_never_generation_retryable(
    error_code: str,
) -> None:
    assert reconciler._raw_sync_error_is_nonretryable(error_code) is True  # noqa: SLF001


def test_native_budget_failure_stops_after_first_generation_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native = tmp_path / "native.jsonl"
    native.write_text("synthetic-safe", encoding="utf-8")
    source = _Source(native)
    config = _Config(tmp_path)
    raw_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_path, config=config)
    store.close()
    reviewed = _reviewed_plan(
        config=config,
        raw_path=raw_path,
        backup_dir=tmp_path / "backups",
        source=source,
    )

    def fail_every_generation(log_service_error, **_kwargs):
        log_service_error(
            "raw_sync:codex",
            NativeArtifactInventoryError("native_freeze_worker_budget_exceeded"),
        )
        return {
            "errors": 1,
            "source_coverage": {
                "sources": {
                    "codex": {
                        "status": "error",
                        "gap": "source_error",
                        "native_sessions": 1,
                        "native_turns": 1,
                        "cursor": {},
                    }
                }
            },
            "source_snapshots": {},
        }

    monkeypatch.setattr(
        reconciler.raw_sync,
        "run_service",
        fail_every_generation,
    )
    identity_plan = {
        "schema_version": "mnemos.raw_session_identity_reconciliation_plan.v1",
        "mode": "append_only_exact_historical_event_set_approval",
        "required_receipt_count": 0,
        "receipt_material_hash": reconciler._canonical_hash([]),  # noqa: SLF001
        "receipts": [],
        "unresolved": [],
        "ok": True,
    }

    with pytest.raises(
        reconciler.AgentSourceRawReconciliationError,
        match="raw_reconciliation_nonretryable_source_failure",
    ):
        reconciler._reconcile_active_source_raw_capture_unlocked(  # noqa: SLF001
            config=config,
            raw_db_path=raw_path,
            backup_dir=tmp_path / "backups",
            sources=[source],
            apply=True,
            cycles=2,
            require_all_active_sources=False,
            runtime_writers_are_inactive=lambda: True,
            session_identity_reconciliation=identity_plan,
            reviewed_before_challenger=reviewed["before_challenger"],
            reviewed_plan_hash="sha256:" + ("e" * 64),
        )

    receipt = json.loads(
        next((tmp_path / "backups").glob("agent-source-raw-reconciliation-*.json")).read_text(
            encoding="utf-8"
        )
    )
    assert [cycle["generation"] for cycle in receipt["cycles"]] == [1]
    receipts = list((tmp_path / "backups").glob("agent-source-raw-reconciliation-*.json"))
    assert len(receipts) == 1
    evidence = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["rollback_ok"] is True
    assert evidence["terminal_failure_codes"] == ["native_freeze_worker_budget_exceeded"]


def test_public_raw_apply_reconciles_identity_receipt_inside_backup_boundary(
    tmp_path: Path,
) -> None:
    native = tmp_path / "native.jsonl"
    native.write_text("synthetic-safe", encoding="utf-8")
    source = _IdentityUpgradeSource(native)
    config = _Config(tmp_path)
    raw_path = tmp_path / "raw_events.db"
    backup_dir = tmp_path / "backups"
    store = RawEventStore(db_path=raw_path, config=config)
    store.upsert_turn(
        source_agent="openclaw",
        session_id="legacy-session",
        turn_number=0,
        user_content="legacy",
        assistant_content="payload",
    )
    store.close()

    plan = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=False,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
    )

    assert plan["apply_eligible"] is True
    assert plan["session_identity_reconciliation"]["required_receipt_count"] == 1
    applied = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )

    assert applied["ok"] is True
    assert applied["required_gap"] == 0
    assert applied["restore_drill_ok"] is True
    assert applied["session_identity_reconciliation"]["ok"] is True
    repeated = reconciler.reconcile_active_source_raw_capture(
        config=config,
        raw_db_path=raw_path,
        backup_dir=backup_dir,
        sources=[source],
        apply=True,
        cycles=2,
        require_all_active_sources=False,
        runtime_writers_are_inactive=lambda: True,
        expected_plan_hash=plan["plan_hash"],
    )
    assert repeated["mode"] == "same_plan_second_apply"
    assert repeated["physical_delta"] == 0
    assert repeated["semantic_delta"] == 0
