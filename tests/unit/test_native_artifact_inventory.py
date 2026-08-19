"""Behavior tests for parser-owned native artifact inventory and freezing."""

from __future__ import annotations

import ast
import sqlite3
import hashlib
import os
import signal
import time
from pathlib import Path

import pytest

from core.sync_framework.agent_source import (
    AgentSource,
    NativeSourceContractError,
    SessionInfo,
    Turn,
)
from core.sync_framework import native_artifact_bounded_parse as bounded_parse_module
from core.sync_framework import native_artifact_inventory as inventory_module
from core.sync_framework.native_artifact_inventory import build_native_artifact_inventory
from core.sync_framework.native_artifact_inventory import (
    NativeArtifactInventoryError,
    freeze_native_sources,
    snapshot_native_sources,
)
from core.sync_framework import native_file_io
from core.sync_framework.native_file_io import (
    copy_native_file_to_descriptor,
    open_native_binary,
    read_native_bytes,
)
from integrations.sources.kimi_source import KimiSource


def test_native_inventory_has_no_lossy_authoritative_path_predicates() -> None:
    module_path = Path(str(inventory_module.__file__ or ""))
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    violations = [
        f"{node.func.attr}:{node.lineno}"
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"exists", "is_file", "is_dir", "is_symlink"}
        )
    ]

    assert violations == []


def test_sqlite_header_read_failure_never_changes_artifact_capability(
    tmp_path: Path,
) -> None:
    from core.sync_framework.native_artifact_inventory import _is_sqlite

    unreadable_as_file = tmp_path / "directory"
    unreadable_as_file.mkdir()

    with pytest.raises(
        NativeArtifactInventoryError,
        match="native_sqlite_header_read_failed",
    ):
        _is_sqlite(unreadable_as_file)


class _MultiArtifactSource(AgentSource):
    name = "codex"
    model_tag = "synthetic"

    def __init__(self, primary: Path, sidecar: Path):
        self.primary = primary
        self.sidecar = sidecar

    def discover_sessions(self):
        return [SessionInfo(session_id="session-1", source_path=self.primary)]

    def native_artifact_paths(self, _session_info: SessionInfo):
        return [self.primary, self.sidecar]

    def parse_turns(self, _session_path: Path):
        return [Turn(turn_number=0, user_content="u", assistant_content="a")]


def test_inventory_binds_side_artifact_bytes_and_opaque_path_identity(tmp_path: Path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    primary = first_root / "session.jsonl"
    sidecar = first_root / "context.jsonl"
    primary.write_text("same-primary", encoding="utf-8")
    sidecar.write_text("side-v1", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)

    first = build_native_artifact_inventory([source])
    sidecar.write_text("side-v2", encoding="utf-8")
    changed_bytes = build_native_artifact_inventory([source])

    moved_primary = second_root / primary.name
    moved_sidecar = second_root / sidecar.name
    moved_primary.write_bytes(primary.read_bytes())
    moved_sidecar.write_bytes(sidecar.read_bytes())
    moved = build_native_artifact_inventory([_MultiArtifactSource(moved_primary, moved_sidecar)])

    assert changed_bytes.inventory_hash != first.inventory_hash
    assert moved.inventory_hash != changed_bytes.inventory_hash
    assert all("first" not in value and "second" not in value for value in moved.path_hashes)


def test_inventory_binds_zero_session_source_and_resolved_root(tmp_path: Path):
    class _EmptySource(AgentSource):
        name = "aider"
        model_tag = "synthetic"

        @property
        def data_dir(self):
            return tmp_path / "absent-aider-root"

        def discover_sessions(self):
            return []

        def parse_turns(self, _session_path: Path):
            return []

    (tmp_path / "absent-aider-root").mkdir()
    inventory = build_native_artifact_inventory([_EmptySource()])
    evidence = inventory.to_evidence()

    assert evidence["source_count"] == 1
    assert evidence["artifact_count"] == 0
    assert evidence["sources"][0]["source_name"] == "aider"
    assert evidence["sources"][0]["session_count"] == 0
    assert evidence["sources"][0]["root_identity_hashes"][0].startswith("sha256:")


def test_kimi_context_parser_declares_every_context_segment(tmp_path: Path):
    artifact_dir = tmp_path / "session"
    artifact_dir.mkdir()
    first = artifact_dir / "context.jsonl"
    second = artifact_dir / "context_1.jsonl"
    wire = artifact_dir / "wire.jsonl"
    for path in (first, second, wire):
        path.write_text("{}\n", encoding="utf-8")
    session = SessionInfo(
        session_id="session",
        source_path=first,
        source_kind="main_context",
    )

    declared = KimiSource().native_artifact_paths(session)

    assert set(declared) == {first, second}
    assert wire not in declared


def test_frozen_source_preserves_the_inventory_root_for_runtime_snapshot(
    tmp_path: Path,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")

    frozen = freeze_native_sources([_MultiArtifactSource(primary, sidecar)])

    assert frozen.sources[0].observed_roots() == [tmp_path.resolve()]
    assert frozen.sources[0].data_dir == tmp_path.resolve()


def test_snapshot_source_preserves_original_root_identity_hashes(
    tmp_path: Path,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)
    before = build_native_artifact_inventory([source])

    with snapshot_native_sources([source]) as snapshot:
        wrapped = build_native_artifact_inventory(snapshot.sources)

        assert snapshot.sources[0].observed_roots() == [tmp_path.resolve()]
        assert snapshot.sources[0].data_dir == tmp_path.resolve()
        assert wrapped.sources[0].root_identity_hashes == (before.sources[0].root_identity_hashes)


def test_freeze_fails_closed_when_reviewed_memory_budget_is_exceeded(
    tmp_path: Path,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")

    with pytest.raises(
        NativeArtifactInventoryError,
        match="native_freeze_(worker_)?budget_exceeded",
    ):
        freeze_native_sources(
            [_MultiArtifactSource(primary, sidecar)],
            max_bytes=1,
        )


def test_freeze_isolates_oversized_parser_output_from_the_parent_process(
    tmp_path: Path,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_bytes(b"x" * 128)
    sidecar.write_bytes(b"y" * 128)
    source = _MultiArtifactSource(primary, sidecar)
    marker = tmp_path / "parser-ran"

    def counted_parse(_path: Path):
        marker.write_text("worker", encoding="utf-8")
        return [
            Turn(
                turn_number=0,
                user_content="u" * (16 * 1024 * 1024),
                assistant_content="a",
            )
        ]

    source.parse_turns = counted_parse  # type: ignore[method-assign]

    with pytest.raises(
        NativeArtifactInventoryError,
        match="native_freeze_(worker_)?budget_exceeded",
    ):
        # Leave enough headroom for deterministic worker startup under a
        # loaded suite while keeping the 16 MiB parser result strictly
        # above both the RSS and materialization budgets.
        freeze_native_sources([source], max_bytes=8 * 1024 * 1024)

    assert marker.read_text(encoding="utf-8") == "worker"


def test_zero_session_source_requires_an_existing_resolved_root(tmp_path: Path):
    class _MissingRootSource(AgentSource):
        name = "aider"
        model_tag = "synthetic"

        @property
        def data_dir(self):
            return tmp_path / "not-installed"

        def discover_sessions(self):
            return []

        def parse_turns(self, _session_path: Path):
            return []

    with pytest.raises(NativeArtifactInventoryError, match="native_root_not_detected"):
        build_native_artifact_inventory([_MissingRootSource()])


def test_zero_session_source_without_any_root_is_not_verified_empty():
    class _NoRootSource(AgentSource):
        name = "aider"
        model_tag = "synthetic"

        def discover_sessions(self):
            return []

        def parse_turns(self, _session_path: Path):
            return []

    with pytest.raises(NativeArtifactInventoryError, match="native_root_not_detected"):
        build_native_artifact_inventory([_NoRootSource()])


def test_freeze_rejects_artifact_drift_during_parse(tmp_path: Path):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("before", encoding="utf-8")

    class _DriftingSource(_MultiArtifactSource):
        def parse_turns(self, _session_path: Path):
            self.sidecar.write_text("after", encoding="utf-8")
            return super().parse_turns(_session_path)

    with pytest.raises(
        NativeArtifactInventoryError,
        match="native_artifact_drift_during_freeze",
    ):
        freeze_native_sources([_DriftingSource(primary, sidecar)])


def test_snapshot_parser_reads_immutable_bytes_across_live_aba_change(
    tmp_path: Path,
):
    primary = tmp_path / "session.jsonl"
    primary.write_text("A", encoding="utf-8")

    class _ReadingSource(AgentSource):
        name = "codex"
        model_tag = "synthetic"

        def __init__(self):
            pass

        @property
        def data_dir(self):
            return tmp_path

        def discover_sessions(self):
            return [SessionInfo(session_id="session-1", source_path=primary)]

        def parse_turns(self, session_path: Path):
            return [
                Turn(
                    turn_number=0,
                    user_content=session_path.read_text(encoding="utf-8"),
                    assistant_content="ok",
                    source_files=[str(session_path), str(tmp_path.parent / "project.py")],
                )
            ]

    source = _ReadingSource()
    with snapshot_native_sources([source]) as snapshot:
        primary.write_text("B", encoding="utf-8")
        primary.write_text("A", encoding="utf-8")
        turns = snapshot.sources[0].parse_session(snapshot.sources[0].discover_sessions()[0])
        parsed_path = next(
            iter(snapshot.sources[0]._snapshot_sessions.values())  # noqa: SLF001
        ).source_path

        assert turns[0].user_content == "A"
        assert turns[0].source_files == [
            str(primary),
            str(tmp_path.parent / "project.py"),
        ]
        assert parsed_path != primary
        assert parsed_path.stat().st_mode & 0o777 == 0o600
        assert parsed_path.parent.stat().st_mode & 0o777 == 0o700

    assert not parsed_path.exists()


def test_snapshot_retries_one_transient_artifact_drift_as_a_whole_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("before", encoding="utf-8")
    original_copy = inventory_module._copy_snapshot_artifact
    drifted = False

    def drifting_copy(source: Path, target: Path):
        nonlocal drifted
        if source == sidecar and not drifted:
            drifted = True
            source.write_text("after", encoding="utf-8")
        return original_copy(source, target)

    monkeypatch.setattr(
        inventory_module,
        "_copy_snapshot_artifact",
        drifting_copy,
    )

    with snapshot_native_sources([_MultiArtifactSource(primary, sidecar)]) as snapshot:
        assert snapshot.stabilization_attempts == 2
        assert snapshot.snapshot_evidence()["stabilization_attempts"] == 2
        turns = snapshot.sources[0].parse_session(snapshot.sources[0].discover_sessions()[0])
        assert turns[0].user_content == "u"


def test_snapshot_rejects_continuous_artifact_drift_after_bounded_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("before", encoding="utf-8")
    original_copy = inventory_module._copy_snapshot_artifact
    drift_count = 0

    def drifting_copy(source: Path, target: Path):
        nonlocal drift_count
        if source == sidecar:
            drift_count += 1
            source.write_text(f"after-{drift_count}", encoding="utf-8")
        return original_copy(source, target)

    monkeypatch.setattr(
        inventory_module,
        "_copy_snapshot_artifact",
        drifting_copy,
    )

    with pytest.raises(
        NativeArtifactInventoryError,
        match="native_artifact_drift_during_snapshot",
    ):
        with snapshot_native_sources(
            [_MultiArtifactSource(primary, sidecar)],
            max_stabilization_attempts=3,
        ):
            raise AssertionError("drifted snapshot must never be yielded")
    assert drift_count == 3


def test_snapshot_rejects_one_session_above_reviewed_input_budget(
    tmp_path: Path,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_bytes(b"x" * 8)
    sidecar.write_bytes(b"y" * 8)

    with pytest.raises(
        NativeArtifactInventoryError,
        match="native_snapshot_session_budget_exceeded",
    ):
        with snapshot_native_sources(
            [_MultiArtifactSource(primary, sidecar)],
            max_session_logical_bytes=15,
        ):
            raise AssertionError("over-budget session must never be yielded")


def test_snapshot_isolates_one_session_parser_output_by_rss_budget(
    tmp_path: Path,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)

    def oversized_parse(_path: Path):
        return [
            Turn(
                turn_number=0,
                user_content="x" * (16 * 1024 * 1024),
                assistant_content="a",
            )
        ]

    source.parse_turns = oversized_parse  # type: ignore[method-assign]
    with snapshot_native_sources(
        [source],
        max_session_parse_bytes=1024 * 1024,
    ) as snapshot:
        with pytest.raises(
            NativeArtifactInventoryError,
            match="native_freeze_(worker_)?budget_exceeded",
        ):
            snapshot.sources[0].parse_session(snapshot.sources[0].discover_sessions()[0])


def test_snapshot_retries_one_signal_terminated_parser_worker(
    tmp_path: Path,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    attempt_marker = tmp_path / "parser-attempted"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)

    def signal_once(_path: Path):
        if not attempt_marker.exists():
            attempt_marker.write_text("first", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGKILL)
        return [Turn(turn_number=0, user_content="u", assistant_content="a")]

    source.parse_turns = signal_once  # type: ignore[method-assign]
    with snapshot_native_sources([source]) as snapshot:
        turns = snapshot.sources[0].parse_session(snapshot.sources[0].discover_sessions()[0])

    assert [turn.turn_number for turn in turns] == [0]
    assert attempt_marker.read_text(encoding="utf-8") == "first"


def test_snapshot_parser_binds_sqlite_and_generic_temp_to_private_spool(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    writable_root = tmp_path / "challenger-writable"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    writable_root.mkdir(mode=0o700)
    source = _MultiArtifactSource(primary, sidecar)

    def temp_root_bound_parse(_path: Path):
        sqlite_temp = Path(os.environ.get("SQLITE_TMPDIR", "")).resolve()
        generic_temp = Path(os.environ.get("TMPDIR", "")).resolve()
        if (
            sqlite_temp != generic_temp
            or sqlite_temp.parent != writable_root.resolve()
            or not sqlite_temp.is_dir()
        ):
            raise NativeSourceContractError("native_test_private_temp_root_unbound")
        return [Turn(turn_number=0, user_content="u", assistant_content="a")]

    source.parse_turns = temp_root_bound_parse  # type: ignore[method-assign]
    with inventory_module.isolated_bounded_parse_spool(writable_root):
        with snapshot_native_sources([source]) as snapshot:
            result = snapshot.sources[0].parse_session_result(
                snapshot.sources[0].discover_sessions()[0]
            )

    assert [turn.turn_number for turn in result.turns] == [0]


def test_snapshot_retries_only_typed_transient_native_storage_failure(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    attempt_log = tmp_path / "parser-attempts"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)

    def transient_once(_path: Path):
        attempts = (
            attempt_log.read_text(encoding="utf-8").splitlines() if attempt_log.exists() else []
        )
        with attempt_log.open("a", encoding="utf-8") as handle:
            handle.write("attempt\n")
        if not attempts:
            failure = sqlite3.OperationalError("sensitive storage failure detail")
            failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
            failure.sqlite_errorname = "SQLITE_BUSY"
            raise NativeSourceContractError.from_storage_failure(
                "native_opencode_artifact_evidence_failed",
                failure,
            )
        return [Turn(turn_number=0, user_content="u", assistant_content="a")]

    source.parse_turns = transient_once  # type: ignore[method-assign]
    with snapshot_native_sources([source]) as snapshot:
        result = snapshot.sources[0].parse_session_result(
            snapshot.sources[0].discover_sessions()[0]
        )

    assert [turn.turn_number for turn in result.turns] == [0]
    assert result.infrastructure_attempt_count == 2
    assert result.recovered_infrastructure_failure == {
        "error_code": "native_session_parser_retryable_exception",
        "exception_type": "NativeSourceContractError",
        "failure_class": "sqlite_transient",
        "reason_code": "native_opencode_artifact_evidence_failed",
        "sqlite_errorcode": sqlite3.SQLITE_BUSY,
        "sqlite_errorname": "SQLITE_BUSY",
    }
    assert attempt_log.read_text(encoding="utf-8").splitlines() == [
        "attempt",
        "attempt",
    ]


def test_snapshot_retries_typed_transient_discovery_as_one_generation(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")

    class TransientDiscoverySource(_MultiArtifactSource):
        attempts = 0

        def discover_sessions(self):
            self.attempts += 1
            if self.attempts == 1:
                failure = sqlite3.OperationalError("sensitive discovery storage detail")
                failure.sqlite_errorcode = sqlite3.SQLITE_LOCKED
                failure.sqlite_errorname = "SQLITE_LOCKED"
                raise NativeSourceContractError.from_storage_failure(
                    "native_cursor_sqlite_discovery_failed",
                    failure,
                )
            return super().discover_sessions()

    source = TransientDiscoverySource(primary, sidecar)
    with snapshot_native_sources([source]) as snapshot:
        assert snapshot.stabilization_attempts == 2
        assert len(snapshot.sources[0].discover_sessions()) == 1

    assert source.attempts >= 4


def test_snapshot_missing_session_identity_has_terminal_attempt_evidence(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)

    with snapshot_native_sources([source]) as snapshot:
        with pytest.raises(
            NativeArtifactInventoryError,
            match="snapshot_session_identity_missing",
        ) as raised:
            snapshot.sources[0].parse_session_result(
                SessionInfo(
                    session_id="missing-session",
                    source_path=primary,
                )
            )

    assert raised.value.details == {"attempt_count": 1}


def test_snapshot_wraps_unregistered_terminal_parse_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)

    with snapshot_native_sources([source]) as snapshot:

        def fail_with_unregistered_code(**_kwargs):
            raise NativeArtifactInventoryError("native_future_unregistered_parse_failure")

        monkeypatch.setattr(
            inventory_module,
            "_bounded_parse_sources",
            fail_with_unregistered_code,
        )
        session = snapshot.sources[0].discover_sessions()[0]
        with pytest.raises(
            NativeArtifactInventoryError,
            match="native_parse_terminal_error_unregistered",
        ) as raised:
            snapshot.sources[0].parse_session_result(session)

    assert raised.value.details == {
        "attempt_count": 1,
        "reason_code": "native_future_unregistered_parse_failure",
    }


def test_snapshot_invalid_recovery_evidence_has_terminal_attempt_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)

    with snapshot_native_sources([source]) as snapshot:

        def fail_without_required_signal(**_kwargs):
            raise NativeArtifactInventoryError("native_freeze_worker_signaled")

        monkeypatch.setattr(
            inventory_module,
            "_bounded_parse_sources",
            fail_without_required_signal,
        )
        session = snapshot.sources[0].discover_sessions()[0]
        with pytest.raises(
            NativeArtifactInventoryError,
            match="native_parse_recovery_evidence_invalid",
        ) as raised:
            snapshot.sources[0].parse_session_result(session)

    assert raised.value.details == {
        "attempt_count": 1,
        "reason_code": "native_freeze_worker_signaled",
    }


def test_snapshot_does_not_retry_nontransient_discovery_failure(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")

    class NontransientDiscoverySource(_MultiArtifactSource):
        attempts = 0

        def discover_sessions(self):
            self.attempts += 1
            failure = sqlite3.OperationalError("sensitive temp path detail")
            failure.sqlite_errorcode = sqlite3.SQLITE_IOERR_GETTEMPPATH
            failure.sqlite_errorname = "SQLITE_IOERR_GETTEMPPATH"
            raise NativeSourceContractError.from_storage_failure(
                "native_opencode_session_discovery_failed",
                failure,
            )

    source = NontransientDiscoverySource(primary, sidecar)
    with pytest.raises(
        NativeArtifactInventoryError,
        match="native_discovery_failed",
    ) as raised:
        with snapshot_native_sources([source]):
            pass

    assert source.attempts == 1
    assert raised.value.details == {
        "failure_class": "sqlite_nontransient",
        "reason_code": "native_opencode_session_discovery_failed",
        "sqlite_errorcode": sqlite3.SQLITE_IOERR_GETTEMPPATH,
        "sqlite_errorname": "SQLITE_IOERR_GETTEMPPATH",
    }


def test_snapshot_nontransient_sqlite_failure_keeps_exact_safe_diagnostics(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    attempt_log = tmp_path / "parser-attempts"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)

    def fail_parse(_path: Path):
        with attempt_log.open("a", encoding="utf-8") as handle:
            handle.write("attempt\n")
        failure = sqlite3.OperationalError("sensitive temp path detail must not escape")
        failure.sqlite_errorcode = sqlite3.SQLITE_IOERR_GETTEMPPATH
        failure.sqlite_errorname = "SQLITE_IOERR_GETTEMPPATH"
        raise NativeSourceContractError.from_storage_failure(
            "native_opencode_artifact_evidence_failed",
            failure,
        )

    source.parse_turns = fail_parse  # type: ignore[method-assign]
    with snapshot_native_sources([source]) as snapshot:
        session = snapshot.sources[0].discover_sessions()[0]
        with pytest.raises(NativeArtifactInventoryError) as raised:
            snapshot.sources[0].parse_session_result(session)

    assert raised.value.code == "native_session_parser_exception"
    assert raised.value.details == {
        "attempt_count": 1,
        "exception_type": "NativeSourceContractError",
        "failure_class": "sqlite_nontransient",
        "reason_code": "native_opencode_artifact_evidence_failed",
        "session_id_hash": ("sha256:" + hashlib.sha256(b"session-1").hexdigest()),
        "source_name": "codex",
        "sqlite_errorcode": sqlite3.SQLITE_IOERR_GETTEMPPATH,
        "sqlite_errorname": "SQLITE_IOERR_GETTEMPPATH",
    }
    assert attempt_log.read_text(encoding="utf-8").splitlines() == ["attempt"]
    assert "sensitive" not in repr(raised.value.details)


def test_snapshot_parser_exception_is_attributed_and_not_retried(
    tmp_path: Path,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    attempt_log = tmp_path / "parser-attempts"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)

    class DeterministicParserFailure(Exception):
        pass

    def deterministic_failure(_path: Path):
        with attempt_log.open("a", encoding="utf-8") as handle:
            handle.write("attempt\n")
        raise DeterministicParserFailure("sensitive parser detail must not escape")

    source.parse_turns = deterministic_failure  # type: ignore[method-assign]
    with snapshot_native_sources([source]) as snapshot:
        session = snapshot.sources[0].discover_sessions()[0]
        with pytest.raises(NativeArtifactInventoryError) as raised:
            snapshot.sources[0].parse_session(session)

    error = raised.value
    assert error.code == "native_session_parser_exception"
    assert str(error) == "native_session_parser_exception"
    assert error.details == {
        "attempt_count": 1,
        "exception_type": "DeterministicParserFailure",
        "session_id_hash": ("sha256:" + hashlib.sha256(b"session-1").hexdigest()),
        "source_name": "codex",
    }
    assert attempt_log.read_text(encoding="utf-8").splitlines() == ["attempt"]
    assert "sensitive" not in repr(error.details)


def test_snapshot_native_contract_failure_preserves_content_free_reason_code(
    tmp_path: Path,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)

    def contract_failure(_path: Path):
        raise NativeSourceContractError("native_opencode_artifact_evidence_failed")

    source.parse_turns = contract_failure  # type: ignore[method-assign]
    with snapshot_native_sources([source]) as snapshot:
        session = snapshot.sources[0].discover_sessions()[0]
        with pytest.raises(NativeArtifactInventoryError) as raised:
            snapshot.sources[0].parse_session(session)

    assert raised.value.code == "native_session_parser_exception"
    assert raised.value.details == {
        "attempt_count": 1,
        "exception_type": "NativeSourceContractError",
        "reason_code": "native_opencode_artifact_evidence_failed",
        "session_id_hash": ("sha256:" + hashlib.sha256(b"session-1").hexdigest()),
        "source_name": "codex",
    }


def test_bounded_parse_failure_reader_rejects_non_hex_session_identity(
    tmp_path: Path,
):
    failure_path = tmp_path / "parse-failure.json"
    failure_path.write_text(
        (
            '{"schema_version":"'
            f'{bounded_parse_module.NATIVE_PARSE_FAILURE_SCHEMA_VERSION}",'
            '"code":"native_session_parser_exception",'
            '"source_name":"codex",'
            f'"session_id_hash":"sha256:{"g" * 64}",'
            '"exception_type":"RuntimeError"}'
        ),
        encoding="utf-8",
    )

    assert inventory_module._read_bounded_parse_failure(failure_path) is None


def test_snapshot_parent_consumes_turn_records_incrementally_near_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)

    def two_turn_parse(_path: Path):
        return [
            Turn(
                turn_number=number,
                user_content=character * (128 * 1024),
                assistant_content="ok",
            )
            for number, character in enumerate(("a", "b"))
        ]

    source.parse_turns = two_turn_parse  # type: ignore[method-assign]
    original_loads = inventory_module.json.loads
    parent_decode_sizes: list[int] = []

    def tracked_loads(value, *args, **kwargs):
        parent_decode_sizes.append(len(value))
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(inventory_module.json, "loads", tracked_loads)
    with snapshot_native_sources(
        [source],
        max_session_parse_bytes=1024 * 1024,
    ) as snapshot:
        turns = snapshot.sources[0].parse_session(snapshot.sources[0].discover_sessions()[0])

    assert [turn.turn_number for turn in turns] == [0, 1]
    assert max(parent_decode_sizes) < 256 * 1024


def test_private_turn_spool_iterator_is_lazy_before_first_record():
    class _GuardedLines:
        def __init__(self):
            self.next_calls = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.next_calls += 1
            if self.next_calls == 1:
                return b"first\n"
            raise StopIteration

    lines = _GuardedLines()
    iterator = inventory_module._iter_spool_lines(lines)  # noqa: SLF001

    assert lines.next_calls == 0
    assert next(iterator) == b"first\n"
    assert lines.next_calls == 1


def test_snapshot_next_run_reaps_private_copy_left_by_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    source = _MultiArtifactSource(primary, sidecar)
    registry = tmp_path / "snapshot-registry"
    monkeypatch.setattr(
        inventory_module,
        "_snapshot_registry_root",
        lambda: registry,
    )

    pid = os.fork()
    if pid == 0:
        with snapshot_native_sources([source]):
            os._exit(86)
    waited_pid, status = os.waitpid(pid, 0)
    assert waited_pid == pid
    assert os.waitstatus_to_exitcode(status) == 86
    assert len(list(registry.glob("snapshot-*"))) == 1

    with snapshot_native_sources([source]) as snapshot:
        assert snapshot.stale_snapshot_dirs_cleaned == 1
        assert len(list(registry.glob("snapshot-*"))) == 1

    assert list(registry.glob("snapshot-*")) == []


def test_parser_spool_next_run_reaps_private_turns_after_parent_sigkill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    registry = tmp_path / "snapshot-registry"
    monkeypatch.setattr(
        inventory_module,
        "_snapshot_registry_root",
        lambda: registry,
    )

    controller = os.fork()
    if controller == 0:
        source = _MultiArtifactSource(primary, sidecar)

        def kill_parser_parent(_path: Path):
            os.kill(os.getppid(), signal.SIGKILL)
            return [
                Turn(
                    turn_number=0,
                    user_content="private-turn-marker",
                    assistant_content="ok",
                )
            ]

        source.parse_turns = kill_parser_parent  # type: ignore[method-assign]
        freeze_native_sources([source])
        os._exit(87)
    waited_pid, status = os.waitpid(controller, 0)
    assert waited_pid == controller
    assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        owner_markers = list(registry.glob("snapshot-*/.owner.json"))
        if owner_markers and all(
            not inventory_module._snapshot_owner_is_live(  # noqa: SLF001
                inventory_module.json.loads(marker.read_text())
            )
            for marker in owner_markers
        ):
            break
        time.sleep(0.01)
    spool_files = list(registry.glob("snapshot-*/turn-spool.ndjson"))
    assert len(spool_files) == 1
    assert b"private-turn-marker" in spool_files[0].read_bytes()

    with snapshot_native_sources([_MultiArtifactSource(primary, sidecar)]) as snapshot:
        assert snapshot.stale_snapshot_dirs_cleaned >= 1

    assert list(registry.glob("snapshot-*")) == []


def test_next_run_terminates_hung_orphan_parser_before_spool_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    primary = tmp_path / "session.jsonl"
    sidecar = tmp_path / "context.jsonl"
    primary.write_text("primary", encoding="utf-8")
    sidecar.write_text("sidecar", encoding="utf-8")
    registry = tmp_path / "snapshot-registry"
    monkeypatch.setattr(
        inventory_module,
        "_snapshot_registry_root",
        lambda: registry,
    )

    controller = os.fork()
    if controller == 0:
        source = _MultiArtifactSource(primary, sidecar)

        def hung_orphan_parser(_path: Path):
            os.kill(os.getppid(), signal.SIGKILL)
            time.sleep(30)
            return []

        source.parse_turns = hung_orphan_parser  # type: ignore[method-assign]
        freeze_native_sources([source])
        os._exit(88)
    waited_pid, status = os.waitpid(controller, 0)
    assert waited_pid == controller
    assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL

    owner_markers = list(registry.glob("snapshot-*/.owner.json"))
    assert len(owner_markers) == 1
    owner = inventory_module.json.loads(owner_markers[0].read_text())
    worker_records = [
        record
        for record in inventory_module._snapshot_owner_records(owner)  # noqa: SLF001
        if record["role"] == "worker"
    ]
    assert len(worker_records) == 1
    worker_record = worker_records[0]

    try:
        started = time.monotonic()
        with snapshot_native_sources([_MultiArtifactSource(primary, sidecar)]) as snapshot:
            assert snapshot.stale_snapshot_dirs_cleaned >= 1
        assert time.monotonic() - started < 5
        assert list(registry.glob("snapshot-*")) == []
        assert inventory_module._matching_owner_process(worker_record) is None  # noqa: SLF001
    finally:
        remaining = inventory_module._matching_owner_process(worker_record)  # noqa: SLF001
        if remaining is not None:
            remaining.kill()
            remaining.wait(timeout=5)


@pytest.mark.parametrize(
    "exit_race",
    ["before_terminate", "before_kill", "after_kill"],
)
def test_abandoned_worker_exit_race_is_already_clean(
    monkeypatch: pytest.MonkeyPatch,
    exit_race: str,
) -> None:
    events: list[str] = []

    class _ExitedWorker:
        pid = 4242
        wait_calls = 0

        def terminate(self) -> None:
            events.append("terminate")
            if exit_race == "before_terminate":
                raise inventory_module.psutil.NoSuchProcess(self.pid)

        def wait(self, timeout: int) -> None:
            events.append("wait")
            self.wait_calls += 1
            if exit_race == "after_kill" and self.wait_calls == 2:
                raise inventory_module.psutil.NoSuchProcess(self.pid)
            raise inventory_module.psutil.TimeoutExpired(timeout, self.pid)

        def kill(self) -> None:
            events.append("kill")
            if exit_race == "before_kill":
                raise inventory_module.psutil.NoSuchProcess(self.pid)

    monkeypatch.setattr(
        inventory_module,
        "_matching_owner_process",
        lambda _record: _ExitedWorker(),
    )

    inventory_module._terminate_abandoned_snapshot_workers(  # noqa: SLF001
        {
            "schema_version": "mnemos.native_artifact_snapshot_owner.v1",
            "owners": [
                {
                    "pid": 4242,
                    "process_create_time": 1.0,
                    "role": "worker",
                }
            ],
        }
    )
    assert (
        events
        == {
            "before_terminate": ["terminate"],
            "before_kill": ["terminate", "wait", "kill"],
            "after_kill": ["terminate", "wait", "kill", "wait"],
        }[exit_race]
    )


@pytest.mark.parametrize(
    ("failure_phase", "expected_error", "expected_events"),
    [
        (
            "terminate_denied",
            "native_snapshot_owner_unverifiable",
            ["terminate"],
        ),
        (
            "first_wait_denied",
            "native_snapshot_owner_unverifiable",
            ["terminate", "wait"],
        ),
        (
            "kill_denied",
            "native_snapshot_owner_unverifiable",
            ["terminate", "wait", "kill"],
        ),
        (
            "final_wait_denied",
            "native_snapshot_owner_unverifiable",
            ["terminate", "wait", "kill", "wait"],
        ),
        (
            "final_wait_timeout",
            "native_snapshot_worker_termination_failed",
            ["terminate", "wait", "kill", "wait"],
        ),
    ],
)
def test_abandoned_worker_action_failure_preserves_private_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
    expected_error: str,
    expected_events: list[str],
) -> None:
    registry = tmp_path / "snapshot-registry"
    stale = registry / "snapshot-action-failure"
    stale.mkdir(parents=True)
    events: list[str] = []

    class _Worker:
        pid = 4242
        wait_calls = 0

        def terminate(self) -> None:
            events.append("terminate")
            if failure_phase == "terminate_denied":
                raise inventory_module.psutil.AccessDenied(self.pid)

        def wait(self, timeout: int) -> None:
            events.append("wait")
            self.wait_calls += 1
            if self.wait_calls == 1:
                if failure_phase == "first_wait_denied":
                    raise inventory_module.psutil.AccessDenied(self.pid)
                raise inventory_module.psutil.TimeoutExpired(timeout, self.pid)
            if failure_phase == "final_wait_denied":
                raise inventory_module.psutil.AccessDenied(self.pid)
            if failure_phase == "final_wait_timeout":
                raise inventory_module.psutil.TimeoutExpired(timeout, self.pid)

        def kill(self) -> None:
            events.append("kill")
            if failure_phase == "kill_denied":
                raise inventory_module.psutil.AccessDenied(self.pid)

    (stale / ".owner.json").write_text(
        inventory_module.json.dumps(
            {
                "schema_version": "mnemos.native_artifact_snapshot_owner.v1",
                "owners": [
                    {
                        "pid": 4000,
                        "process_create_time": 1.0,
                        "role": "controller",
                    },
                    {
                        "pid": 4242,
                        "process_create_time": 1.0,
                        "role": "worker",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    worker = _Worker()
    monkeypatch.setattr(
        inventory_module,
        "_matching_owner_process",
        lambda record: worker if record.get("role") == "worker" else None,
    )

    with pytest.raises(
        NativeArtifactInventoryError,
        match=expected_error,
    ):
        inventory_module._cleanup_stale_snapshot_dirs_locked(registry)  # noqa: SLF001

    assert events == expected_events
    assert stale.is_dir()


def test_snapshot_cleanup_fails_closed_when_owner_liveness_is_unverifiable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    registry = tmp_path / "snapshot-registry"
    stale = registry / "snapshot-live-unverifiable"
    stale.mkdir(parents=True)
    registry.chmod(0o700)
    stale.chmod(0o700)
    (stale / ".owner.json").write_text(
        inventory_module.json.dumps(
            {
                "schema_version": "mnemos.native_artifact_snapshot_owner.v1",
                "owners": [
                    {
                        "pid": os.getpid(),
                        "process_create_time": 1.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        inventory_module,
        "_snapshot_registry_root",
        lambda: registry,
    )

    class _DeniedProcess:
        def __init__(self, _pid: int):
            raise inventory_module.psutil.AccessDenied()

    monkeypatch.setattr(inventory_module.psutil, "Process", _DeniedProcess)
    with pytest.raises(
        NativeArtifactInventoryError,
        match="native_snapshot_owner_unverifiable",
    ):
        inventory_module._create_registered_snapshot_root()  # noqa: SLF001

    assert stale.is_dir()


def test_snapshot_cleanup_never_deletes_uninspectable_owner_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "snapshot-registry"
    stale = registry / "snapshot-owner-uninspectable"
    stale.mkdir(parents=True)
    marker = stale / ".owner.json"
    marker.write_text("{}", encoding="utf-8")
    original_lstat = Path.lstat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == marker:
            raise PermissionError("sentinel")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)

    with pytest.raises(
        NativeArtifactInventoryError,
        match="native_snapshot_owner_invalid",
    ):
        inventory_module._cleanup_stale_snapshot_dirs_locked(registry)  # noqa: SLF001

    assert stale.is_dir()
    assert marker.read_bytes() == b"{}"


def test_snapshot_copy_never_overwrites_uninspectable_existing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.jsonl"
    target = tmp_path / "snapshot" / "target.jsonl"
    source.write_bytes(b"new")
    target.parent.mkdir()
    target.write_bytes(b"preexisting")
    original_lstat = Path.lstat
    original_open = os.open
    target_open_attempts: list[Path] = []

    def denied(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise PermissionError("sentinel")
        return original_lstat(path, *args, **kwargs)

    def tracked_open(
        path: Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(path) == target:
            target_open_attempts.append(Path(path))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "lstat", denied)
        scoped.setattr(inventory_module.os, "open", tracked_open)
        with pytest.raises(
            NativeArtifactInventoryError,
            match="native_artifact_snapshot_failed",
        ):
            inventory_module._copy_snapshot_artifact(  # noqa: SLF001
                source,
                target,
            )

    assert target.read_bytes() == b"preexisting"
    assert target_open_attempts == []


def test_snapshot_copy_collision_never_deletes_or_overwrites_foreign_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.jsonl"
    target = tmp_path / "snapshot" / "target.jsonl"
    source.write_bytes(b"new")
    target.parent.mkdir()
    original_open = os.open
    collided = False

    def collide_before_exclusive_create(
        path: Path,
        flags: int,
        mode: int = 0o777,
    ) -> int:
        nonlocal collided
        candidate = Path(path)
        if candidate == target and flags & os.O_EXCL and not collided:
            descriptor = original_open(
                candidate,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.write(descriptor, b"foreign-snapshot")
            os.close(descriptor)
            collided = True
        return original_open(candidate, flags, mode)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            inventory_module.os,
            "open",
            collide_before_exclusive_create,
        )
        with pytest.raises(
            NativeArtifactInventoryError,
            match="native_artifact_snapshot_failed",
        ):
            inventory_module._copy_snapshot_artifact(  # noqa: SLF001
                source,
                target,
            )

    assert collided is True
    assert target.read_bytes() == b"foreign-snapshot"


def test_snapshot_copy_closes_source_when_target_sqlite_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "snapshot" / "target.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")

    class SourceConnection:
        closed = False

        def close(self) -> None:
            self.closed = True

    source_connection = SourceConnection()
    failure = sqlite3.OperationalError("private target open failure")
    failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
    failure.sqlite_errorname = "SQLITE_BUSY"
    monkeypatch.setattr(
        inventory_module,
        "connect_native_sqlite_readonly",
        lambda _path: source_connection,
    )
    monkeypatch.setattr(
        inventory_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(
        NativeArtifactInventoryError,
        match="native_storage_transient_failure",
    ):
        inventory_module._copy_snapshot_artifact(source, target)  # noqa: SLF001

    assert source_connection.closed is True
    assert not target.exists()


def test_snapshot_owner_marker_publish_fsyncs_parent_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Path]] = []
    original_replace = inventory_module.os.replace

    def tracked_replace(source, destination):
        original_replace(source, destination)
        events.append(("replace", Path(destination)))

    monkeypatch.setattr(inventory_module.os, "replace", tracked_replace)
    monkeypatch.setattr(
        inventory_module,
        "fsync_directory",
        lambda path: events.append(("fsync", Path(path))),
    )

    inventory_module._write_snapshot_owner_marker(  # noqa: SLF001
        tmp_path,
        (os.getpid(),),
    )

    assert events[-2:] == [
        ("replace", tmp_path / ".owner.json"),
        ("fsync", tmp_path),
    ]


def test_snapshot_uses_sqlite_backup_api_and_preserves_reviewed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database = tmp_path / "session.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE messages (body TEXT NOT NULL)")
        connection.execute("INSERT INTO messages VALUES ('A')")

    class _SqliteSource(AgentSource):
        name = "crush"
        model_tag = "synthetic"

        @property
        def data_dir(self):
            return tmp_path

        def discover_sessions(self):
            return [SessionInfo(session_id="session-1", source_path=database)]

        def parse_turns(self, session_path: Path):
            with sqlite3.connect(
                f"{session_path.resolve().as_uri()}?mode=ro",
                uri=True,
            ) as connection:
                value = connection.execute("SELECT body FROM messages").fetchone()[0]
            return [Turn(turn_number=0, user_content=value, assistant_content="ok")]

    helper_reads: list[Path] = []
    original_readonly = inventory_module.connect_native_sqlite_readonly

    def tracked_readonly(path: Path, **kwargs):
        helper_reads.append(Path(path).resolve())
        return original_readonly(path, **kwargs)

    monkeypatch.setattr(
        inventory_module,
        "connect_native_sqlite_readonly",
        tracked_readonly,
    )

    with snapshot_native_sources([_SqliteSource()]) as snapshot:
        wrapped = snapshot.sources[0]
        snapshot_root = wrapped.snapshot_read_roots()[0]
        snapshot_database = snapshot_root / database.name
        assert [path.name for path in snapshot_root.iterdir()] == [database.name]
        with inventory_module.connect_native_sqlite_readonly(
            snapshot_database,
            immutable=True,
        ) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        before = (
            hashlib.sha256(snapshot_database.read_bytes()).hexdigest(),
            snapshot_database.stat().st_mtime_ns,
        )
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE messages SET body = 'B'")
        turns = wrapped.parse_session(wrapped.discover_sessions()[0])
        after = (
            hashlib.sha256(snapshot_database.read_bytes()).hexdigest(),
            snapshot_database.stat().st_mtime_ns,
        )
        assert after == before
        assert [path.name for path in snapshot_root.iterdir()] == [database.name]
        assert snapshot.snapshot_evidence()["sqlite_snapshot_journal_mode"] == "delete"
        assert snapshot.snapshot_evidence()["sqlite_snapshot_sidecar_count"] == 0

    assert turns[0].user_content == "A"
    assert database.resolve() in helper_reads


def test_native_file_reader_preserves_exact_bytes_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"\x00\xffnative\n")
    alias = tmp_path / "artifact-link.bin"
    alias.symlink_to(artifact)

    assert read_native_bytes(artifact) == b"\x00\xffnative\n"
    with pytest.raises(OSError):
        read_native_bytes(alias)


def test_native_file_reader_rejects_path_replacement_after_descriptor_read(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"original")
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement")

    with pytest.raises(OSError, match="native_artifact_changed_during_read"):
        with open_native_binary(artifact) as handle:
            assert handle.read() == b"original"
            os.replace(replacement, artifact)

    assert artifact.read_bytes() == b"replacement"


def test_native_file_copy_consumes_short_writes_without_losing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes((b"0123456789" * 1000) + b"tail")
    target = tmp_path / "target.bin"
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    original_write = native_file_io.os.write

    def short_write(fd: int, content: bytes) -> int:
        return original_write(fd, content[: max(1, len(content) // 3)])

    monkeypatch.setattr(native_file_io.os, "write", short_write)
    try:
        copied = copy_native_file_to_descriptor(source, descriptor)
    finally:
        os.close(descriptor)

    assert copied == source.stat().st_size
    assert target.read_bytes() == source.read_bytes()


def test_native_inventory_rejects_leaf_symlink_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.jsonl"
    artifact.write_text("{}\n", encoding="utf-8")
    alias = tmp_path / "artifact-link.jsonl"
    alias.symlink_to(artifact)
    source = _MultiArtifactSource(alias, artifact)

    with pytest.raises(
        NativeArtifactInventoryError,
        match="native_artifact_unreadable",
    ):
        build_native_artifact_inventory([source])
