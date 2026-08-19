# -*- coding: utf-8 -*-
"""Tests for the independent Native-to-Raw challenger."""

from __future__ import annotations

import os
from pathlib import Path
import weakref

import pytest

from core.agent_kit import native_raw_challenger
from core.agent_kit.native_raw_challenger import audit_native_to_raw
from core.sync_framework.agent_source import (
    AgentSource,
    SessionInfo,
    SessionParseResult,
    Turn,
)
from core.sync_framework.native_artifact_inventory import (
    NativeArtifactInventoryError,
    snapshot_native_sources,
)
from daemon.raw_only_sync_engine import RawOnlySyncEngine
from core.sync_framework.raw_event_store import RawEventStore


class _Config:
    def __init__(self, root: Path):
        self.database_dir = root
        self.data_dir = root

    def get(self, _key, default=None):
        return default


class _Source(AgentSource):
    name = "codex"
    model_tag = "synthetic-codex"

    def __init__(self, path: Path, turns: list[Turn]):
        self.path = path
        self.turns = turns

    def discover_sessions(self):
        return [SessionInfo(session_id="session", source_path=self.path)]

    def parse_turns(self, _path: Path):
        return list(self.turns)

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


def _turn(number: int, identity: str) -> Turn:
    return Turn(
        turn_number=number,
        user_content=f"synthetic-user-{number}",
        assistant_content=f"synthetic-assistant-{number}",
        native_event_id=identity,
    )


def test_challenger_worker_pipe_write_retries_short_progress_and_rejects_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks: list[bytes] = []

    def short_write(_descriptor: int, content: bytes) -> int:
        written = min(2, len(content))
        chunks.append(bytes(content[:written]))
        return written

    monkeypatch.setattr(native_raw_challenger.os, "write", short_write)
    native_raw_challenger._write_all(7, b"abcdef")  # noqa: SLF001
    assert b"".join(chunks) == b"abcdef"

    monkeypatch.setattr(native_raw_challenger.os, "write", lambda _fd, _value: 0)
    with pytest.raises(OSError, match="worker write made no progress"):
        native_raw_challenger._write_all(7, b"blocked")  # noqa: SLF001


def test_active_scope_keeps_the_twelve_source_denominator(tmp_path: Path):
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=_Config(tmp_path)).close()

    report = audit_native_to_raw(
        [],
        raw_db_path=raw_path,
        require_all_host_sources=True,
        source_scope="active",
    )

    assert report["schema_version"] == ("mnemos.agent_source_native_raw_challenger.v3")
    assert report["source_scope"] == "active"
    assert len(report["blocking_sources"]) == 12
    assert "aider" in report["blocking_sources"]
    assert "codex" in report["blocking_sources"]


def test_uninspectable_raw_database_is_not_reported_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_path = tmp_path / "raw_events.db"
    original_stat = Path.stat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == raw_path:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    _events, errors = native_raw_challenger._visible_raw_events(
        raw_path,
        "codex",
    )

    assert errors == ["raw_database_unavailable"]


def test_challenger_detects_a_missing_native_raw_event_then_closes_after_raw_only_write(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    source = _Source(source_path, [_turn(0, "native-0"), _turn(1, "native-1")])
    raw_path = tmp_path / "raw_events.db"
    engine = RawOnlySyncEngine(raw_store=RawEventStore(db_path=raw_path, config=config))
    try:
        engine.sync_turns(
            source,
            source.discover_sessions()[0],
            source.turns[:1],
            incremental=False,
            enqueue_distillation=False,
        )
        missing = audit_native_to_raw(
            [source], raw_db_path=raw_path, require_all_host_sources=False
        )
        assert missing["ok"] is False
        assert missing["sources"]["codex"]["expected_visible_missing"] == 1

        engine.sync_turns(
            source,
            source.discover_sessions()[0],
            source.turns[1:],
            incremental=False,
            enqueue_distillation=False,
        )
        complete = audit_native_to_raw(
            [source], raw_db_path=raw_path, require_all_host_sources=False
        )
    finally:
        engine.close()

    assert complete["ok"] is True, complete
    assert complete["sources"]["codex"]["expected_visible_match"] == 2
    assert complete["sources"]["codex"]["visible_unobserved_or_legacy"] == 0
    assert complete["sources"]["codex"]["native_session_turn_upper_bound"] == 2


def test_challenger_releases_prior_session_turns_before_next_parser_call(
    tmp_path: Path,
    monkeypatch,
):
    class NestedMetadata:
        pass

    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    source = _Source(source_path, [])
    source.discover_sessions = lambda: [
        SessionInfo(session_id="first", source_path=source_path),
        SessionInfo(session_id="second", source_path=source_path),
    ]
    prior_turn: weakref.ReferenceType[Turn] | None = None
    prior_nested_metadata: weakref.ReferenceType[NestedMetadata] | None = None
    seen_before_parser: list[tuple[bool, bool]] = []
    call_count = 0

    def parse_one_session(_source, _session):
        nonlocal prior_turn, prior_nested_metadata, call_count
        seen = (
            prior_turn is not None and prior_turn() is not None,
            (prior_nested_metadata is not None and prior_nested_metadata() is not None),
        )
        seen_before_parser.append(seen)
        if seen[0]:
            raise RuntimeError("prior session turns are still live")
        if seen[1]:
            raise RuntimeError("prior session turn metadata is still live")
        call_count += 1
        turn = _turn(0, f"native-{call_count}")
        nested_metadata = NestedMetadata()
        turn.metadata["nested"] = {"retained": nested_metadata}
        prior_turn = weakref.ref(turn)
        prior_nested_metadata = weakref.ref(nested_metadata)
        return [turn]

    monkeypatch.setattr(
        native_raw_challenger,
        "parse_discovered_session_result",
        lambda source, session: SessionParseResult(
            turns=tuple(parse_one_session(source, session)),
            disposition="parsed",
            reason_code="native_turns_parsed",
        ),
    )
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=_Config(tmp_path)).close()

    report = audit_native_to_raw(
        [source],
        raw_db_path=raw_path,
        require_all_host_sources=False,
    )

    assert report["sources"]["codex"]["errors"] == []
    assert report["sources"]["codex"]["native_parsed_turns"] == 2
    assert seen_before_parser == [(False, False), (False, False)]
    assert call_count == 2


def test_challenger_preserves_content_free_parser_failure_evidence(
    tmp_path: Path,
):
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    source = _Source(source_path, [])

    def fail_with_typed_evidence(_path: Path):
        raise NativeArtifactInventoryError(
            "native_session_parser_exception",
            details={
                "attempt_count": 1,
                "exception_type": "NativeSourceContractError",
                "reason_code": "native_opencode_artifact_evidence_failed",
                "session_id_hash": "sha256:" + ("f" * 64),
                "source_name": "spoofed-source",
            },
        )

    source.parse_turns = fail_with_typed_evidence  # type: ignore[method-assign]
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=_Config(tmp_path)).close()

    report = audit_native_to_raw(
        [source],
        raw_db_path=raw_path,
        require_all_host_sources=False,
    )

    codex = report["sources"]["codex"]
    expected_session_hash = "sha256:" + native_raw_challenger.hashlib.sha256(b"session").hexdigest()
    assert codex["errors"] == ["native_session_parse_failed"]
    assert codex["native_session_parse_failures"] == [
        {
            "attempt_count": 1,
            "error_code": "native_session_parser_exception",
            "exception_type": "NativeSourceContractError",
            "reason_code": "native_opencode_artifact_evidence_failed",
            "session_id_hash": expected_session_hash,
            "source_name": "codex",
        }
    ]


def test_challenger_types_unexpected_parser_failure_without_native_content(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    source = _Source(source_path, [])

    def fail_without_typed_evidence(_path: Path):
        raise RuntimeError("native transcript bytes must not escape")

    source.parse_turns = fail_without_typed_evidence  # type: ignore[method-assign]
    raw_path = tmp_path / "raw_events.db"
    RawEventStore(db_path=raw_path, config=_Config(tmp_path)).close()

    report = audit_native_to_raw(
        [source],
        raw_db_path=raw_path,
        require_all_host_sources=False,
    )

    codex = report["sources"]["codex"]
    assert codex["errors"] == ["native_session_parse_failed"]
    assert codex["native_session_parse_failures"] == [
        {
            "attempt_count": 1,
            "error_code": "native_session_parser_exception",
            "exception_type": "RuntimeError",
            "session_id_hash": (
                "sha256:" + native_raw_challenger.hashlib.sha256(b"session").hexdigest()
            ),
            "source_name": "codex",
        }
    ]
    assert "native transcript bytes must not escape" not in str(codex)


def test_snapshot_challenger_computes_turn_identities_in_one_session_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    source = _Source(source_path, [_turn(0, "native-0")])
    controller_pid = os.getpid()
    original = native_raw_challenger._expected_event_identity  # noqa: SLF001

    def require_session_worker(*args, **kwargs):
        if os.getpid() == controller_pid:
            raise AssertionError("turn identity materialization leaked into long-lived challenger")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        native_raw_challenger,
        "_expected_event_identity",
        require_session_worker,
    )
    with snapshot_native_sources([source]) as snapshot:
        expected, summary, errors = native_raw_challenger._expected_native_events(  # noqa: SLF001
            snapshot.sources[0]
        )

    assert len(expected) == 1
    assert summary["native_parsed_turns"] == 1
    assert summary["native_identity_isolated_sessions"] == 1
    assert errors == []


def test_snapshot_challenger_preserves_identity_worker_failure_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    source = _Source(source_path, [_turn(0, "native-0")])
    monkeypatch.setattr(
        native_raw_challenger,
        "_SESSION_IDENTITY_WORKER_MAX_BYTES",
        1,
    )

    with snapshot_native_sources([source]) as snapshot:
        _expected, summary, errors = native_raw_challenger._expected_native_events(  # noqa: SLF001
            snapshot.sources[0]
        )

    assert errors == ["native_session_parse_failed"]
    assert summary["native_session_parse_failures"] == [
        {
            "attempt_count": 1,
            "error_code": "native_session_parser_exception",
            "exception_type": "NativeRawChallengerError",
            "reason_code": "native_session_identity_worker_budget_exceeded",
            "session_id_hash": (
                "sha256:" + native_raw_challenger.hashlib.sha256(b"session").hexdigest()
            ),
            "source_name": "codex",
        }
    ]


def test_challenger_rejects_duplicate_native_logical_identity_even_when_raw_count_matches(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    source = _Source(source_path, [_turn(0, "native-0"), _turn(1, "native-0")])
    raw_path = tmp_path / "raw_events.db"
    engine = RawOnlySyncEngine(raw_store=RawEventStore(db_path=raw_path, config=config))
    try:
        engine.sync_turns(
            source,
            source.discover_sessions()[0],
            source.turns[:1],
            incremental=False,
            enqueue_distillation=False,
        )
        report = audit_native_to_raw([source], raw_db_path=raw_path, require_all_host_sources=False)
    finally:
        engine.close()

    assert report["ok"] is False
    assert report["sources"]["codex"]["native_identity_duplicates"] == 1
    assert "native_logical_identity_duplicate" in report["sources"]["codex"]["errors"]


def test_challenger_replays_raw_source_artifact_identity_not_metadata_alone(
    tmp_path: Path,
):
    config = _Config(tmp_path)
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    source = _Source(
        source_path,
        [
            Turn(
                turn_number=0,
                user_content="synthetic-user",
                assistant_content="synthetic-assistant",
            )
        ],
    )
    raw_path = tmp_path / "raw_events.db"
    engine = RawOnlySyncEngine(raw_store=RawEventStore(db_path=raw_path, config=config))
    try:
        engine.sync_turns(
            source,
            source.discover_sessions()[0],
            source.turns,
            incremental=False,
            enqueue_distillation=False,
        )
        report = audit_native_to_raw([source], raw_db_path=raw_path, require_all_host_sources=False)
    finally:
        engine.close()

    assert report["ok"] is True, report
    assert report["sources"]["codex"]["native_identity_kinds"] == {"parser_artifact_offset": 1}
    assert report["sources"]["codex"]["native_legacy_identity_turns"] == 0
