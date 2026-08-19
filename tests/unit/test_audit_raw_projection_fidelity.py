"""Lossless Raw projection reverse-parser tests."""

from __future__ import annotations

from argparse import Namespace
import base64
import gzip
import hashlib
import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from core.sync_framework.raw_event_store import RawEventStore
from scripts import audit_raw_projection_fidelity as fidelity
from scripts import project_raw_vault as projection
from scripts.audit_raw_projection_fidelity import (
    _canonical_turns,
    audit_raw_projection_fidelity,
)


class _Config:
    def __init__(self, database_dir: Path):
        self.database_dir = database_dir
        self.data_dir = database_dir

    def get(self, _key, default=None):
        return default


def test_managed_projection_inventory_rejects_invalid_utf8_marker(
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    candidate = raw_dir / "corrupt.md"
    candidate.write_bytes(
        b"---\nmnemos_type: raw_retention_projection\n---\n\xff\n"
    )

    with pytest.raises(
        ValueError,
        match="managed Raw projection candidate is not valid UTF-8",
    ):
        fidelity._safe_managed_projection_paths(raw_dir)  # noqa: SLF001


def _project(
    tmp_path: Path,
    *,
    user_content: str = "user content\n### raw heading remains bytes",
    source_agent: str = "codex",
    session_id: str = "session-1",
    turn_count: int = 1,
) -> tuple[Path, Path]:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        for turn_number in range(turn_count):
            suffix = "" if turn_number == 0 else f" {turn_number}"
            store.upsert_turn(
                source_agent=source_agent,
                session_id=session_id,
                turn_number=turn_number,
                user_content=user_content + suffix,
                assistant_content="assistant content" + suffix,
                reasoning="reasoning content" + suffix,
                tool_calls=[{"name": "safe-tool", "arguments": {"x": turn_number}}],
                tool_results=[{"status": "ok", "turn_number": turn_number}],
                timestamp=f"2026-06-28T10:00:{turn_number:02d}",
            )
        chunks = projection.build_projection_chunks(
            projection._fetch_refs(store),  # noqa: SLF001
            chunk_turns=5,
            max_chunks=None,
        )
        projection.write_projection(raw_dir, store, chunks, db_path=db_path, max_turn_chars=0)
    finally:
        store.close()
    return raw_dir, db_path


def _rewrite_projection_journal(raw_dir: Path, mutator) -> None:
    journal_path = raw_dir / projection.PROJECTION_JOURNAL_NAME
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    mutator(payload)
    payload["generation_hash"] = hashlib.sha256(
        json.dumps(
            payload["files"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    journal_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _rebind_projection_file(raw_dir: Path, projection_file: Path) -> None:
    relative_path = projection_file.relative_to(raw_dir).as_posix()

    def bind_file(payload):
        payload["files"][relative_path]["content_hash"] = hashlib.sha256(
            projection_file.read_bytes()
        ).hexdigest()

    _rewrite_projection_journal(raw_dir, bind_file)


def test_reverse_parser_matches_every_visible_canonical_raw_field(tmp_path: Path):
    raw_dir, db_path = _project(tmp_path)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is True, report
    assert report["missing_event_ids"] == 0
    assert report["duplicate_event_ids"] == 0
    assert report["truncated_events"] == 0
    assert report["truncated_marker_files"] == 0
    assert report["field_hash_mismatch_count"] == 0
    assert report["visible_fields_checked"] == 4


@pytest.mark.parametrize(
    "hostile_timestamp",
    (
        '2026-07-29`"\\line\n<!-- --> 雪',
        '../../escape`"\\line\n<!-- --> 雪',
    ),
)
def test_reviewed_apply_round_trips_delimiter_hostile_native_timestamps(
    tmp_path: Path,
    hostile_timestamp: str,
) -> None:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        store.upsert_turn(
            source_agent="kimi",
            session_id="native-timestamp-round-trip",
            turn_number=0,
            user_content="preserve native metadata",
            assistant_content="without breaking projection grammar",
            timestamp=hostile_timestamp,
        )
        chunks = projection.build_projection_chunks(
            projection._fetch_refs(store),  # noqa: SLF001
            chunk_turns=1,
            max_chunks=None,
        )
        stats = {
            "raw_dir": str(raw_dir),
            "db_path": str(db_path),
            "projection_plan": projection.build_projection_plan(
                raw_dir,
                store,
                chunks,
                db_path=db_path,
                max_turn_chars=0,
            ),
        }
        applied = projection.apply_projection(
            Namespace(backup_dir="", max_turn_chars=0),
            store,
            chunks,
            stats,
        )
        assert applied["post_apply_zero_delta"] is True
    finally:
        store.close()

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is True, report
    projection_text = next(raw_dir.rglob("*.md")).read_text(encoding="utf-8")
    assert hostile_timestamp not in projection_text
    assert "\\n" in projection_text
    assert not (tmp_path / "escape").exists()


def test_reverse_parser_keeps_safe_legacy_backtick_headers_compatible(
    tmp_path: Path,
) -> None:
    raw_dir, db_path = _project(tmp_path)
    projection_file = next(raw_dir.rglob("*.md"))
    text = projection_file.read_text(encoding="utf-8")
    text = text.replace(
        '- captured_at: "',
        "- captured_at: `",
    ).replace(
        '"\n- conversation_at: "',
        "`\n- conversation_at: `",
        1,
    ).replace(
        '"\n- completeness: "complete"',
        "`\n- completeness: `complete`",
        1,
    )
    projection_file.write_text(text, encoding="utf-8")
    _rebind_projection_file(raw_dir, projection_file)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is True, report


def test_runtime_projection_reference_drift_is_not_hidden_by_visible_field_hashes(
    tmp_path: Path,
):
    raw_dir, db_path = _project(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE raw_metrics
            SET search_count=search_count + 3,
                result_count=result_count + 2,
                hit_count=hit_count + 1,
                reference_count=reference_count + 4,
                survival_score=0.876
            """
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
        include_gap_evidence=True,
    )

    assert report["ok"] is False
    assert report["projection_reference_mismatch_count"] == 1, report
    assert report["gap_generation"]["projection_reference_mismatch_count"] == 1
    assert len(report["projection_reference_mismatch_evidence"]) == 1
    mismatch = report["projection_reference_mismatch_evidence"][0]
    assert mismatch["revision_id"].startswith("rawrev-")
    assert mismatch["expected_projection_reference_hash"] != (
        mismatch["observed_projection_reference_hash"]
    )
    assert report["gap_generation"]["classification"] == (
        "projection_content_or_structure_invalid"
    )


def test_projection_frontmatter_metric_aggregate_drift_fails_closed(
    tmp_path: Path,
) -> None:
    raw_dir, db_path = _project(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE raw_metrics
            SET view_count=9, freshness_score=0.76543, confidence=0.87654
            """
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
        include_gap_evidence=True,
    )

    assert report["ok"] is False
    assert report["projection_metric_aggregate_mismatch_count"] == 1
    assert len(report["projection_metric_aggregate_mismatch_evidence"]) == 1
    evidence = report["projection_metric_aggregate_mismatch_evidence"][0]
    assert evidence["relative_path"].endswith(".md")
    assert evidence["expected_projection_metric_aggregate_hash"] != (
        evidence["observed_projection_metric_aggregate_hash"]
    )


def test_canonical_comparison_cache_keeps_hashes_not_visible_raw_bodies(tmp_path: Path):
    _raw_dir, db_path = _project(tmp_path)

    expected = _canonical_turns(db_path, include_eligible_delete=False)

    assert len(expected) == 1
    fields = next(iter(expected.values()))
    assert set(fields) == {"user_content", "assistant_content", "reasoning", "structured"}
    assert all(len(value) == 64 for value in fields.values())
    assert "user content" not in str(fields)


def test_fidelity_denominator_excludes_nonconforming_native_contract(tmp_path: Path):
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        healthy = store.upsert_turn(
            source_agent="opencode",
            session_id="fidelity-contract",
            turn_number=0,
            user_content="healthy user",
            assistant_content="healthy assistant",
            metadata={"native_event_id": "healthy-native"},
        )
        quarantined = store.upsert_turn(
            source_agent="opencode",
            session_id="fidelity-contract",
            turn_number=1,
            user_content="quarantined user",
            assistant_content="quarantined assistant",
            metadata={"native_event_id": "quarantined-native"},
        )
        logical_event_id = store.get_turn(quarantined)["logical_event_id"]
        conn = store._pool.get_conn()  # noqa: SLF001
        ledger = NativeRawContractLedger()
        ledger.record_explicit(
            conn,
            logical_event_id=logical_event_id,
            revision_id=quarantined,
            support_manifest_hash="test-native-contract-manifest",
            contract_state="nonconforming",
            contract_errors=["cross_session_native_identity"],
            observed_at="2026-07-13T00:00:00+00:00",
        )
        ledger.refresh_effective_state(
            conn,
            logical_event_id=logical_event_id,
            observed_at="2026-07-13T00:00:00+00:00",
        )
        conn.commit()

        chunks = projection.build_projection_chunks(
            projection._fetch_refs(store),  # noqa: SLF001
            chunk_turns=5,
            max_chunks=None,
        )
        projection.write_projection(raw_dir, store, chunks, db_path=db_path, max_turn_chars=0)
    finally:
        store.close()

    expected = _canonical_turns(db_path, include_eligible_delete=False)
    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert set(expected) == {healthy}
    assert report["ok"] is True, report
    assert report["expected_event_ids"] == 1
    assert report["observed_event_ids"] == 1


def test_reverse_parser_treats_marker_literals_inside_raw_as_visible_evidence(tmp_path: Path):
    raw_dir, db_path = _project(
        tmp_path,
        user_content=(
            "literal marker text follows\n"
            "[... projection truncated; canonical raw_events.db has full text ...]\n"
            "<!-- mnemos-raw-event-v2 not-a-structural-marker -->"
        ),
    )

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is True, report
    assert report["truncated_marker_files"] == 0


def test_reverse_parser_detects_mutated_visible_raw_bytes(tmp_path: Path):
    raw_dir, db_path = _project(tmp_path)
    projection_file = next(raw_dir.rglob("*.md"))
    projection_file.write_text(
        projection_file.read_text(encoding="utf-8").replace("assistant content", "tampered textdata"),
        encoding="utf-8",
    )

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["field_hash_mismatch_count"] == 1


def test_gap_evidence_freezes_new_and_replaced_revisions_without_raw_bodies(
    tmp_path: Path,
):
    raw_dir, db_path = _project(tmp_path, user_content="projected original body")
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        replacement = store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=0,
            user_content="replacement secret body",
            assistant_content="assistant content",
            reasoning="reasoning content",
            timestamp="2026-06-28T10:00:00",
        )
        added = store.upsert_turn(
            source_agent="kimi",
            session_id="session-new",
            turn_number=0,
            user_content="new secret body",
            assistant_content="new assistant",
            timestamp="2026-06-29T10:00:00",
        )
    finally:
        store.close()

    default_report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)
    evidence_report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
        include_gap_evidence=True,
    )

    assert default_report["missing_event_ids"] == 2
    assert default_report["unexpected_event_ids"] == 1
    assert "missing_revision_evidence" not in default_report
    gap = evidence_report["gap_generation"]
    assert gap["classification"] == "projection_generation_stale"
    assert gap["missing_new_logical_event_count"] == 1
    assert gap["missing_replacement_revision_count"] == 1
    assert gap["unexpected_superseded_revision_count"] == 1
    assert gap["unknown_unexpected_revision_count"] == 0
    assert len(gap["gap_hash"]) == 64
    assert len(gap["missing_revision_evidence_hash"]) == 64
    assert len(gap["unexpected_revision_evidence_hash"]) == 64
    assert {item["revision_id"] for item in evidence_report["missing_revision_evidence"]} == {
        replacement,
        added,
    }
    assert all(
        set(item["visible_field_hashes"]) == set(projection.VISIBLE_FIELDS)
        for item in evidence_report["missing_revision_evidence"]
    )
    serialized = str(evidence_report)
    assert "replacement secret body" not in serialized
    assert "new secret body" not in serialized


def test_missing_revision_evidence_hash_covers_rows_beyond_public_samples(
    tmp_path: Path,
):
    raw_dir, db_path = _project(tmp_path)
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        for turn_number in range(25):
            store.upsert_turn(
                source_agent="kimi",
                session_id="full-missing-denominator",
                turn_number=turn_number,
                user_content=f"missing user {turn_number}",
                assistant_content=f"missing assistant {turn_number}",
            )
    finally:
        store.close()

    report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
        include_gap_evidence=True,
    )

    evidence = report["missing_revision_evidence"]
    assert len(evidence) == 25
    assert len(report["missing_event_id_samples"]) == 20
    full_hash = fidelity._canonical_json_hash(evidence)  # noqa: SLF001
    sampled_hash = fidelity._canonical_json_hash(evidence[:20])  # noqa: SLF001
    assert report["gap_generation"]["missing_revision_evidence_hash"] == full_hash
    assert full_hash != sampled_hash


def test_gap_generation_hash_is_stable_across_repeated_read_only_audits(tmp_path: Path):
    raw_dir, db_path = _project(tmp_path)
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        store.upsert_turn(
            source_agent="kimi",
            session_id="unprojected",
            turn_number=0,
            user_content="not published",
            assistant_content="not published",
        )
    finally:
        store.close()

    first = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)
    second = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert first["gap_generation"] == second["gap_generation"]


def test_gap_audit_does_not_create_or_modify_formal_state(tmp_path: Path):
    raw_dir, db_path = _project(tmp_path)

    def snapshot() -> dict[str, str]:
        return {
            str(path.relative_to(tmp_path)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(tmp_path.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)
    after = snapshot()

    assert report["ok"] is True, report
    assert after == before


def test_gap_audit_fails_closed_on_live_wal_without_touching_sidecars(tmp_path: Path):
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="active-wal",
            turn_number=0,
            user_content="live user",
            assistant_content="live assistant",
        )
        wal_path = db_path.with_name(db_path.name + "-wal")
        assert wal_path.stat().st_size > 0

        def sidecars() -> dict[str, str]:
            return {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (wal_path, db_path.with_name(db_path.name + "-shm"))
                if path.is_file()
            }

        before = sidecars()
        report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)
        after = sidecars()
    finally:
        store.close()

    assert report["ok"] is False
    assert "checkpointed immutable evidence-epoch snapshot" in report["error"]
    assert after == before


def test_gap_audit_rejects_projection_generation_change_during_read(
    tmp_path: Path,
    monkeypatch,
):
    raw_dir, db_path = _project(tmp_path)
    original = fidelity._projection_inventory  # noqa: SLF001
    calls = 0

    def changing_inventory(path: Path, relative_paths: list[str]):
        nonlocal calls
        calls += 1
        inventory = original(path, relative_paths)
        if calls == 2:
            inventory = {**inventory, "concurrent-change": {"exists": True}}
        return inventory

    monkeypatch.setattr(fidelity, "_projection_inventory", changing_inventory)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == "evidence_epoch_changed"
    assert report["gap_generation"]["evidence_epoch_stable"] is False


def test_superseded_unexpected_revision_must_still_match_its_canonical_bytes(
    tmp_path: Path,
):
    raw_dir, db_path = _project(tmp_path, user_content="projected original body")
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=0,
            user_content="replacement current body",
            assistant_content="assistant content",
            reasoning="reasoning content",
            timestamp="2026-06-28T10:00:00",
        )
    finally:
        store.close()
    projection_file = next(raw_dir.rglob("*.md"))
    projection_file.write_text(
        projection_file.read_text(encoding="utf-8").replace("original", "modified"),
        encoding="utf-8",
    )

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["field_hash_mismatch_count"] == 1
    assert (
        report["gap_generation"]["unexpected_superseded_field_mismatch_count"]
        == 1
    )
    assert (
        report["gap_generation"]["classification"]
        == "projection_content_or_structure_invalid"
    )


def test_superseded_unexpected_revision_must_match_canonical_runtime_reference(
    tmp_path: Path,
) -> None:
    raw_dir, db_path = _project(tmp_path)
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=0,
            user_content="replacement current body",
            assistant_content="assistant content",
            reasoning="reasoning content",
            timestamp="2026-06-28T10:00:00",
        )
    finally:
        store.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE raw_metrics SET reference_count=reference_count + 999")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
        include_gap_evidence=True,
    )

    assert report["ok"] is False
    assert report["projection_reference_mismatch_count"] == 1
    assert len(report["projection_reference_mismatch_evidence"]) == 1
    assert report["projection_reference_mismatch_evidence"][0][
        "revision_id"
    ].startswith("rawrev-")
    assert report["gap_generation"]["unexpected_superseded_revision_count"] == 0
    assert report["gap_generation"]["classification"] == (
        "projection_content_or_structure_invalid"
    )
    assert "unexpected_revision_projection_reference_mismatch" in report["errors"]


def test_revision_marker_logical_event_id_must_match_canonical_lineage(tmp_path: Path):
    raw_dir, db_path = _project(tmp_path)
    projection_file = next(raw_dir.rglob("*.md"))
    text = projection_file.read_text(encoding="utf-8")
    marker = next(
        line for line in text.splitlines() if line.startswith(projection.EVENT_MARKER_PREFIX)
    )
    marker_payload = json.loads(
        marker[len(projection.EVENT_MARKER_PREFIX) : -4]
    )
    marker_payload["logical_event_id"] = "f" * 32
    replacement = (
        projection.EVENT_MARKER_PREFIX
        + json.dumps(
            marker_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + " -->"
    )
    projection_file.write_text(text.replace(marker, replacement), encoding="utf-8")
    relative_path = projection_file.relative_to(raw_dir).as_posix()

    def mutate(payload):
        metadata = payload["files"][relative_path]
        metadata["content_hash"] = hashlib.sha256(projection_file.read_bytes()).hexdigest()
        metadata["logical_event_ids"] = ["f" * 32]

    _rewrite_projection_journal(raw_dir, mutate)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["logical_event_id_mismatch_count"] == 1
    assert (
        report["gap_generation"]["classification"]
        == "projection_content_or_structure_invalid"
    )


@pytest.mark.parametrize(
    "mutation",
    ["missing", "corrupt", "generation_hash", "content_hash", "revision_set_hash"],
)
def test_gap_generation_requires_valid_bound_publisher_journal(
    tmp_path: Path,
    mutation: str,
):
    raw_dir, db_path = _project(tmp_path)
    journal_path = raw_dir / projection.PROJECTION_JOURNAL_NAME
    if mutation == "missing":
        journal_path.unlink()
    elif mutation == "corrupt":
        journal_path.write_text("{not-json", encoding="utf-8")
    elif mutation == "generation_hash":
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        payload["generation_hash"] = "f" * 64
        journal_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        def mutate(payload):
            metadata = next(iter(payload["files"].values()))
            metadata[mutation] = "f" * 64

        _rewrite_projection_journal(raw_dir, mutate)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] != "in_sync"
    assert any("projection_journal_" in error for error in report["errors"])


def test_publisher_journal_membership_binding_does_not_assume_marker_order(
    tmp_path: Path,
):
    raw_dir, db_path = _project(tmp_path, turn_count=2)

    def reverse_metadata_order(payload):
        metadata = next(iter(payload["files"].values()))
        assert len(metadata["revision_ids"]) == 2
        metadata["revision_ids"] = list(reversed(metadata["revision_ids"]))
        metadata["logical_event_ids"] = list(reversed(metadata["logical_event_ids"]))
        metadata["revision_set_hash"] = hashlib.sha256(
            json.dumps(
                metadata["revision_ids"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    _rewrite_projection_journal(raw_dir, reverse_metadata_order)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is True
    assert report["gap_generation"]["classification"] == "in_sync"


def test_malformed_journal_membership_lists_fail_closed_without_traceback(
    tmp_path: Path,
):
    raw_dir, db_path = _project(tmp_path)

    def drop_logical_membership(payload):
        metadata = next(iter(payload["files"].values()))
        metadata["logical_event_ids"] = []

    _rewrite_projection_journal(raw_dir, drop_logical_membership)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == (
        "projection_content_or_structure_invalid"
    )
    assert any(
        error.startswith("projection_journal_metadata_mismatch:")
        for error in report["errors"]
    )


@pytest.mark.parametrize("escape_kind", ["journal", "chunk", "parent"])
def test_projection_audit_rejects_symlink_escape(
    tmp_path: Path,
    escape_kind: str,
):
    raw_dir, db_path = _project(tmp_path)
    projection_file = next(raw_dir.rglob("*.md"))
    if escape_kind == "journal":
        source = raw_dir / projection.PROJECTION_JOURNAL_NAME
        external = tmp_path / "external-journal.json"
    elif escape_kind == "chunk":
        source = projection_file
        external = tmp_path / "external-projection.md"
    else:
        relative = projection_file.relative_to(raw_dir)
        source = raw_dir / relative.parts[0]
        external = tmp_path / "external-projection-parent"
    source.rename(external)
    source.symlink_to(external)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == (
        "projection_content_or_structure_invalid"
    )
    assert any("path_unsafe" in error for error in report["errors"])


@pytest.mark.parametrize("placement", ("trailing", "inter_event"))
def test_projection_parser_rejects_unconsumed_bytes_even_with_rehashed_journal(
    tmp_path: Path,
    placement: str,
):
    raw_dir, db_path = _project(
        tmp_path,
        turn_count=2 if placement == "inter_event" else 1,
    )
    projection_file = next(raw_dir.rglob("*.md"))
    projected = projection_file.read_bytes()
    if placement == "trailing":
        projected += b"FORGED EXTRA BODY NOT IN CANONICAL RAW\n"
        expected_error = "unconsumed_trailing_bytes"
    else:
        first_header = projected.find(b"\n## Turn ")
        second_header = projected.find(b"\n## Turn ", first_header + 1)
        assert first_header >= 0 and second_header > first_header
        projected = (
            projected[:second_header]
            + b"\nFORGED INTER-EVENT BODY NOT IN CANONICAL RAW\n"
            + projected[second_header:]
        )
        expected_error = "unconsumed_inter_event_bytes"
    projection_file.write_bytes(projected)
    relative_path = projection_file.relative_to(raw_dir).as_posix()

    def bind_forged_file(payload):
        payload["files"][relative_path]["content_hash"] = hashlib.sha256(
            projection_file.read_bytes()
        ).hexdigest()

    _rewrite_projection_journal(raw_dir, bind_forged_file)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == (
        "projection_content_or_structure_invalid"
    )
    assert any(expected_error in error for error in report["errors"])


def test_projection_journal_cannot_repoint_chunk_to_noncanonical_contained_path(
    tmp_path: Path,
):
    raw_dir, db_path = _project(tmp_path)
    original = next(raw_dir.rglob("*.md"))
    forged = raw_dir / "evil" / "forged_t0001-0001.md"
    forged.parent.mkdir()
    original.rename(forged)
    original_relative = original.relative_to(raw_dir).as_posix()
    forged_relative = forged.relative_to(raw_dir).as_posix()

    def repoint(payload):
        payload["files"][forged_relative] = payload["files"].pop(
            original_relative
        )

    _rewrite_projection_journal(raw_dir, repoint)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == (
        "projection_content_or_structure_invalid"
    )
    assert any(
        "projection_preamble_canonical_aggregate_mismatch" in error
        for error in report["errors"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "quoted_extra_key",
        "yaml_comment",
        "duplicate_key",
        "event_marker_extra_member",
        "missing_metric_lines",
        "huge_score",
        "canonical_db_type",
        "turn_range",
        "conversation_range",
        "completeness_set",
    ],
)
def test_projection_grammar_and_stable_aggregates_are_exact(
    tmp_path: Path,
    mutation: str,
):
    raw_dir, db_path = _project(tmp_path)
    projection_file = next(raw_dir.rglob("*.md"))
    text = projection_file.read_text(encoding="utf-8")
    if mutation == "quoted_extra_key":
        text = text.replace("---\n", '---\n"raw_body": "SECRET"\n', 1)
    elif mutation == "yaml_comment":
        text = text.replace(
            "projection_version: 2\n",
            "projection_version: 2\n# SECRET RAW BODY IN YAML COMMENT\n",
            1,
        )
    elif mutation == "duplicate_key":
        text = text.replace(
            'mnemos_type: "raw_retention_projection"\n',
            'mnemos_type: "raw_retention_projection"\n'
            'mnemos_type: "raw_retention_projection"\n',
            1,
        )
    elif mutation == "event_marker_extra_member":
        marker = next(
            line
            for line in text.splitlines()
            if line.startswith(projection.EVENT_MARKER_PREFIX)
        )
        marker_payload = json.loads(
            marker[len(projection.EVENT_MARKER_PREFIX) : -4]
        )
        marker_payload["raw_body"] = "SECRET"
        forged = (
            projection.EVENT_MARKER_PREFIX
            + json.dumps(
                marker_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + " -->"
        )
        text = text.replace(marker, forged, 1)
    elif mutation == "missing_metric_lines":
        text = text.replace(
            "- survival_score: `1.00`\n"
            "- search/result/hit/reference: `0/0/0/0`\n",
            "",
            1,
        )
    elif mutation == "huge_score":
        text = text.replace(
            "freshness_score: 1.0\n",
            "freshness_score: " + ("9" * 1000) + "\n",
            1,
        )
    elif mutation == "canonical_db_type":
        text = text.replace(
            f'canonical_db: "{db_path}"\n',
            "canonical_db: []\n",
            1,
        )
    elif mutation == "turn_range":
        text = text.replace("turn_start: 1\nturn_end: 1\n", "turn_start: 999\nturn_end: 1000\n", 1)
    elif mutation == "conversation_range":
        text = text.replace(
            'conversation_start_at: "2026-06-28T10:00:00"\n',
            'conversation_start_at: "2099-01-01T00:00:00"\n',
            1,
        )
    else:
        text = text.replace(
            "completeness_statuses:\n  - complete\n",
            "completeness_statuses:\n  - forged\n",
            1,
        )
    projection_file.write_text(text, encoding="utf-8")
    _rebind_projection_file(raw_dir, projection_file)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == (
        "projection_content_or_structure_invalid"
    )
    assert "SECRET" not in json.dumps(report, ensure_ascii=False)


def test_duplicate_and_deep_projection_journal_fail_closed_without_traceback(
    tmp_path: Path,
):
    raw_dir, db_path = _project(tmp_path)
    journal_path = raw_dir / projection.PROJECTION_JOURNAL_NAME
    original = journal_path.read_text(encoding="utf-8")
    duplicate = original.replace(
        "{",
        '{"schema_version":"mnemos.raw_projection.v2",',
        1,
    )
    journal_path.write_text(duplicate, encoding="utf-8")
    duplicate_report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
    )
    journal_path.write_text(
        '{"nested":' + ("[" * 2000) + "0" + ("]" * 2000) + "}",
        encoding="utf-8",
    )
    deep_report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
    )

    for report in (duplicate_report, deep_report):
        assert report["ok"] is False
        assert any(
            error
            in {
                "projection_journal_unreadable",
                "projection_journal_contract_mismatch",
            }
            for error in report["errors"]
        )


def test_missing_only_replacement_requires_real_direct_predecessor(tmp_path: Path):
    raw_dir, db_path = _project(tmp_path)
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        store.upsert_turn(
            source_agent="kimi",
            session_id="never-projected",
            turn_number=0,
            user_content="original",
            assistant_content="assistant",
        )
        current = store.upsert_turn(
            source_agent="kimi",
            session_id="never-projected",
            turn_number=0,
            user_content="replacement",
            assistant_content="assistant",
        )
    finally:
        store.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE raw_turn_revisions
            SET supersedes_revision_id=?
            WHERE revision_id=?
            """,
            ("rawrev-" + ("f" * 40), current),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
        include_gap_evidence=True,
    )

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == "audit_input_unreadable"
    assert "invalid direct predecessor" in report["error"]


def test_historical_replacement_requires_real_direct_predecessor(tmp_path: Path):
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="historical-lineage",
            turn_number=0,
            user_content="revision zero",
            assistant_content="assistant",
        )
        projected_revision = store.upsert_turn(
            source_agent="codex",
            session_id="historical-lineage",
            turn_number=0,
            user_content="revision one",
            assistant_content="assistant",
        )
        chunks = projection.build_projection_chunks(
            projection._fetch_refs(store),  # noqa: SLF001
            chunk_turns=5,
            max_chunks=None,
        )
        projection.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
        store.upsert_turn(
            source_agent="codex",
            session_id="historical-lineage",
            turn_number=0,
            user_content="revision two",
            assistant_content="assistant",
        )
    finally:
        store.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE raw_turn_revisions
            SET supersedes_revision_id=?
            WHERE revision_id=?
            """,
            ("rawrev-" + ("f" * 40), projected_revision),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] != "projection_generation_stale"
    assert any("invalid direct predecessor" in error for error in report["errors"])


def test_cross_linked_current_pointer_cannot_remove_logical_event_from_denominator(
    tmp_path: Path,
):
    raw_dir, db_path = _project(tmp_path)
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        second = store.upsert_turn(
            source_agent="kimi",
            session_id="cross-link",
            turn_number=0,
            user_content="second event",
            assistant_content="assistant",
        )
    finally:
        store.close()
    with sqlite3.connect(db_path) as conn:
        first_revision = conn.execute(
            """
            SELECT current_revision_id
            FROM raw_turns
            WHERE event_id != (
                SELECT logical_event_id
                FROM raw_turn_revisions
                WHERE revision_id=?
            )
            LIMIT 1
            """,
            (second,),
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE raw_turns
            SET current_revision_id=?
            WHERE current_revision_id=?
            """,
            (first_revision, second),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == "audit_input_unreadable"
    assert "cross-linked current owner" in report["error"]


@pytest.mark.parametrize(
    "invalid_pointer",
    ["rawrev-" + ("f" * 40), "", None],
)
def test_invalid_current_pointer_cannot_remove_logical_event_from_denominator(
    tmp_path: Path,
    invalid_pointer: str | None,
):
    raw_dir, db_path = _project(tmp_path)
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        second = store.upsert_turn(
            source_agent="kimi",
            session_id="dangling-pointer",
            turn_number=0,
            user_content="second event",
            assistant_content="assistant",
        )
    finally:
        store.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE raw_turns
            SET current_revision_id=?
            WHERE current_revision_id=?
            """,
            (invalid_pointer, second),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == "audit_input_unreadable"
    assert "invalid " in report["error"]


def test_projection_sequence_is_bound_to_canonical_turn_order(tmp_path: Path):
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        for turn_number in (0, 1):
            store.upsert_turn(
                source_agent="codex",
                session_id="sequence",
                turn_number=turn_number,
                user_content=f"user {turn_number}",
                assistant_content=f"assistant {turn_number}",
            )
        chunks = projection.build_projection_chunks(
            projection._fetch_refs(store),  # noqa: SLF001
            chunk_turns=5,
            max_chunks=None,
        )
        projection.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
    finally:
        store.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE raw_turns
            SET turn_number=CASE turn_number WHEN 0 THEN 1 ELSE 0 END
            WHERE session_id='sequence'
            """
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == (
        "projection_content_or_structure_invalid"
    )
    assert any(
        "projection_preamble_canonical_aggregate_mismatch" in error
        for error in report["errors"]
    )


def test_deep_canonical_snapshot_fails_closed_without_traceback(tmp_path: Path):
    raw_dir, db_path = _project(tmp_path)
    with sqlite3.connect(db_path) as conn:
        revision_id = conn.execute(
            "SELECT current_revision_id FROM raw_turns LIMIT 1"
        ).fetchone()[0]
        depth_bomb = ("[" * 2000) + "0" + ("]" * 2000)
        conn.execute(
            "UPDATE raw_turn_revisions SET snapshot_blob=? WHERE revision_id=?",
            (sqlite3.Binary(zlib.compress(depth_bomb.encode())), revision_id),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == "audit_input_unreadable"
    assert "snapshot is " in report["error"]


def test_deep_historical_snapshot_fails_closed_without_traceback(tmp_path: Path):
    raw_dir, db_path = _project(tmp_path)
    with sqlite3.connect(db_path) as conn:
        projected_revision = conn.execute(
            "SELECT current_revision_id FROM raw_turns LIMIT 1"
        ).fetchone()[0]
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=0,
            user_content="replacement",
            assistant_content="assistant content",
            reasoning="reasoning content",
            timestamp="2026-06-28T10:00:00",
        )
    finally:
        store.close()
    with sqlite3.connect(db_path) as conn:
        snapshot = zlib.decompress(
            conn.execute(
                "SELECT snapshot_blob FROM raw_turn_revisions WHERE revision_id=?",
                (projected_revision,),
            ).fetchone()[0]
        ).decode("utf-8")
        nested = ("[" * 2000) + "0" + ("]" * 2000)
        original_snapshot = snapshot
        for original in ('"raw_event_refs":[]', '"raw_event_refs": []'):
            snapshot = snapshot.replace(
                original,
                f'"raw_event_refs":{nested}',
                1,
            )
        assert snapshot != original_snapshot
        conn.execute(
            "UPDATE raw_turn_revisions SET snapshot_blob=? WHERE revision_id=?",
            (
                sqlite3.Binary(zlib.compress(snapshot.encode("utf-8"))),
                projected_revision,
            ),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] != "projection_generation_stale"
    assert any("snapshot is unreadable" in error for error in report["errors"])


@pytest.mark.parametrize("revision_scope", ["current", "historical"])
def test_duplicate_snapshot_json_keys_fail_closed(
    tmp_path: Path,
    revision_scope: str,
):
    raw_dir, db_path = _project(tmp_path)
    with sqlite3.connect(db_path) as conn:
        projected_revision = conn.execute(
            "SELECT current_revision_id FROM raw_turns LIMIT 1"
        ).fetchone()[0]
    if revision_scope == "historical":
        store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
        try:
            store.upsert_turn(
                source_agent="codex",
                session_id="session-1",
                turn_number=0,
                user_content="replacement",
                assistant_content="assistant content",
                reasoning="reasoning content",
                timestamp="2026-06-28T10:00:00",
            )
        finally:
            store.close()
    with sqlite3.connect(db_path) as conn:
        target_revision = (
            projected_revision
            if revision_scope == "historical"
            else conn.execute(
                "SELECT current_revision_id FROM raw_turns LIMIT 1"
            ).fetchone()[0]
        )
        snapshot = zlib.decompress(
            conn.execute(
                "SELECT snapshot_blob FROM raw_turn_revisions WHERE revision_id=?",
                (target_revision,),
            ).fetchone()[0]
        ).decode("utf-8")
        snapshot = snapshot.replace(
            "{",
            '{"user_content":"SECRET DUPLICATE",',
            1,
        )
        conn.execute(
            "UPDATE raw_turn_revisions SET snapshot_blob=? WHERE revision_id=?",
            (
                sqlite3.Binary(zlib.compress(snapshot.encode("utf-8"))),
                target_revision,
            ),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] != "projection_generation_stale"
    assert "SECRET DUPLICATE" not in json.dumps(report, ensure_ascii=False)
    assert any("snapshot is unreadable" in error for error in report["errors"])


@pytest.mark.parametrize("revision_scope", ["current", "historical"])
def test_revision_metadata_cannot_smuggle_raw_body_into_gap_evidence(
    tmp_path: Path,
    revision_scope: str,
):
    raw_dir, db_path = _project(tmp_path)
    original_revision = next(
        iter(
            json.loads(
                (raw_dir / projection.PROJECTION_JOURNAL_NAME).read_text(
                    encoding="utf-8"
                )
            )["files"].values()
        )
    )["revision_ids"][0]
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        current_revision = store.upsert_turn(
            source_agent="codex",
            session_id="session-1" if revision_scope == "historical" else "new-session",
            turn_number=0,
            user_content="replacement" if revision_scope == "historical" else "new",
            assistant_content="assistant",
            reasoning="reasoning content",
            timestamp="2026-06-28T10:00:00",
        )
    finally:
        store.close()
    target_revision = (
        original_revision if revision_scope == "historical" else current_revision
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE raw_turn_revisions
            SET content_hash=?, full_content_hash=?
            WHERE revision_id=?
            """,
            ("SECRET RAW BODY", "ANOTHER SECRET RAW BODY", target_revision),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
        include_gap_evidence=True,
    )
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] != "projection_generation_stale"
    assert "SECRET RAW BODY" not in serialized
    assert "ANOTHER SECRET RAW BODY" not in serialized
    assert any("invalid content digest" in error for error in report["errors"])


def test_legacy_revision_digest_shapes_remain_valid_content_free_metadata(
    tmp_path: Path,
):
    raw_dir, db_path = _project(tmp_path)
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        revision_id = store.upsert_turn(
            source_agent="kimi",
            session_id="legacy-digest",
            turn_number=0,
            user_content="new",
            assistant_content="new",
        )
    finally:
        store.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE raw_turn_revisions
            SET content_hash=?, full_content_hash=''
            WHERE revision_id=?
            """,
            ("a" * 16, revision_id),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
        include_gap_evidence=True,
    )

    assert report["gap_generation"]["classification"] == "projection_generation_stale"
    evidence = next(
        item
        for item in report["missing_revision_evidence"]
        if item["revision_id"] == revision_id
    )
    assert evidence["content_hash"] == "a" * 16
    assert evidence["full_content_hash"] == ""


def test_projection_preamble_canonical_db_is_bound_to_audited_identity(
    tmp_path: Path,
):
    raw_dir, db_path = _project(tmp_path)
    projection_file = next(raw_dir.rglob("*.md"))
    alternate_root = tmp_path / "not-the-audited"
    alternate_root.mkdir()
    alternate_db = alternate_root / "raw_events.db"
    text = projection_file.read_text(encoding="utf-8")
    projection_file.write_text(
        text.replace(
            f'canonical_db: "{db_path}"',
            f'canonical_db: "{alternate_db}"',
        ),
        encoding="utf-8",
    )
    relative_path = projection_file.relative_to(raw_dir).as_posix()

    def bind_forged_file(payload):
        payload["files"][relative_path]["content_hash"] = hashlib.sha256(
            projection_file.read_bytes()
        ).hexdigest()

    _rewrite_projection_journal(raw_dir, bind_forged_file)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == (
        "projection_content_or_structure_invalid"
    )
    assert any(
        "projection_preamble_contract_mismatch" in error
        for error in report["errors"]
    )


def test_projection_preamble_accepts_equivalent_resolved_canonical_db_identity(
    tmp_path: Path,
):
    raw_dir, db_path = _project(tmp_path)
    alias_dir = tmp_path / "canonical-db-alias"
    alias_dir.symlink_to(tmp_path)
    alias_db_path = alias_dir / db_path.name
    projection_file = next(raw_dir.rglob("*.md"))
    projection_file.write_text(
        projection_file.read_text(encoding="utf-8").replace(
            f'canonical_db: "{db_path}"',
            f'canonical_db: "{alias_db_path}"',
        ),
        encoding="utf-8",
    )
    relative_path = projection_file.relative_to(raw_dir).as_posix()

    def bind_alias_path(payload):
        payload["files"][relative_path]["content_hash"] = hashlib.sha256(
            projection_file.read_bytes()
        ).hexdigest()

    _rewrite_projection_journal(raw_dir, bind_alias_path)

    report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
        canonical_db_identity=db_path,
    )

    assert report["ok"] is True, report
    assert report["gap_generation"]["classification"] == "in_sync"


@pytest.mark.parametrize(
    ("source_agent", "session_id"),
    [
        (
            "claude",
            "99c38c4b-971e-481a-88bc-6b61a0495130::subagent::ee7616ba62ae5fea1934ee61",
        ),
        (
            "crush",
            "4e35340c-0ab4-4486-84e5-ec288263dc1c$$tool_kmeArLVDTvwcgOJjQxE1vPCP",
        ),
        (
            "file_ingestor:trusted_user_document",
            "checkpoint.jsonl::artifact::47a9a953beb5674e0ca0",
        ),
    ],
)
def test_projection_preamble_accepts_canonical_historical_source_identities(
    tmp_path: Path,
    source_agent: str,
    session_id: str,
):
    raw_dir, db_path = _project(
        tmp_path,
        source_agent=source_agent,
        session_id=session_id,
    )

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is True, report
    assert report["gap_generation"]["classification"] == "in_sync"


def test_cog009_gap_evidence_archive_is_complete_reconstructable_and_content_free():
    artifact_path = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "acceptance"
        / "cog009_raw_projection_reference_gap_evidence_20260725.json"
    )
    artifact_bytes = artifact_path.read_bytes()
    assert artifact_bytes.endswith(b"\n")
    artifact = fidelity._strict_json_loads(  # noqa: SLF001
        artifact_bytes[:-1].decode("utf-8")
    )
    canonical_artifact = json.dumps(
        artifact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert artifact_bytes == canonical_artifact + b"\n"
    encoded_archive = artifact["evidence_archive"]["payload_base64"]
    archive_bytes = base64.b64decode(encoded_archive, validate=True)
    assert base64.b64encode(archive_bytes).decode("ascii") == encoded_archive
    decoded = gzip.decompress(
        archive_bytes
    )
    evidence = json.loads(decoded)
    canonical_decoded = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert set(artifact) == {
        "schema_version",
        "source_audit_schema",
        "source_snapshot_db_sha256",
        "gap_generation",
        "counts",
        "evidence_archive",
    }
    assert artifact["schema_version"] == (
        "mnemos.cog009_raw_projection_gap_evidence.v3"
    )
    assert artifact["source_audit_schema"] == fidelity.SCHEMA_VERSION
    assert artifact["source_snapshot_db_sha256"] == (
        "99251f42e05d61c59a8488c2e488fc514e94f48a0a8baad25e657a8fae91781e"
    )
    assert set(artifact["evidence_archive"]) == {
        "encoding",
        "decoded_sha256",
        "payload_base64",
    }
    assert artifact["evidence_archive"]["encoding"] == (
        "gzip+base64(canonical-json)"
    )
    assert decoded == canonical_decoded
    assert hashlib.sha256(decoded).hexdigest() == (
        artifact["evidence_archive"]["decoded_sha256"]
    )
    assert artifact["evidence_archive"]["decoded_sha256"] == (
        "abc32d9d368311d5d53e6e37997d50a873a033f0ebbcb2d873e10f1009317837"
    )
    assert set(evidence) == {
        "missing_revision_evidence",
        "projection_metric_aggregate_mismatch_evidence",
        "projection_reference_mismatch_evidence",
        "structural_error_evidence",
        "unexpected_revision_evidence",
    }
    assert len(evidence["missing_revision_evidence"]) == 497
    assert len(evidence["unexpected_revision_evidence"]) == 1
    assert len(evidence["structural_error_evidence"]) == 2
    assert len(evidence["projection_reference_mismatch_evidence"]) == 7603
    assert len(evidence["projection_metric_aggregate_mismatch_evidence"]) == 2691
    assert artifact["counts"] == {
        "error_count": 10794,
        "expected_event_ids": 24678,
        "field_hash_mismatch_count": 0,
        "logical_event_id_mismatch_count": 0,
        "missing_event_ids": 497,
        "observed_event_ids": 24182,
        "paired_superseded_revision_count": 1,
        "projection_metadata_mismatch_count": 0,
        "projection_metric_aggregate_mismatch_count": 2691,
        "projection_reference_mismatch_count": 7603,
        "structural_error_count": 2,
        "truncated_marker_files": 0,
        "unexpected_event_ids": 1,
        "unpaired_superseded_revision_count": 0,
    }
    assert set(artifact["gap_generation"]) == {
        "canonical_db_identity_hash",
        "classification",
        "evidence_epoch_stable",
        "expected_revision_evidence_hash",
        "expected_revision_set_hash",
        "gap_hash",
        "logical_event_id_mismatch_count",
        "missing_new_logical_event_count",
        "missing_replacement_revision_count",
        "missing_revision_evidence_hash",
        "observed_revision_evidence_hash",
        "observed_revision_set_hash",
        "paired_superseded_revision_count",
        "projection_metadata_mismatch_count",
        "projection_metric_aggregate_mismatch_count",
        "projection_metric_aggregate_mismatch_evidence_hash",
        "projection_reference_mismatch_count",
        "projection_reference_mismatch_evidence_hash",
        "publisher_generation_hash",
        "publisher_journal_hash",
        "structural_error_count",
        "structural_error_evidence_hash",
        "unexpected_revision_evidence_hash",
        "unexpected_superseded_field_mismatch_count",
        "unexpected_superseded_revision_count",
        "unknown_unexpected_revision_count",
        "unpaired_superseded_revision_count",
    }
    assert artifact["gap_generation"]["gap_hash"] == (
        "b489932a25db550e781b825ade166417ba651db07571a3ed05f5ec032a3adebf"
    )
    assert artifact["gap_generation"]["classification"] == (
        "projection_content_or_structure_invalid"
    )
    assert artifact["gap_generation"]["evidence_epoch_stable"] is True
    assert artifact["counts"]["expected_event_ids"] - artifact["counts"][
        "observed_event_ids"
    ] == 496
    assert artifact["counts"]["error_count"] == (
        artifact["counts"]["missing_event_ids"]
        + artifact["counts"]["unexpected_event_ids"]
        + artifact["counts"]["projection_metric_aggregate_mismatch_count"]
        + artifact["counts"]["projection_reference_mismatch_count"]
        + artifact["counts"]["structural_error_count"]
    )
    assert fidelity._canonical_json_hash(  # noqa: SLF001
        evidence["missing_revision_evidence"]
    ) == artifact["gap_generation"]["missing_revision_evidence_hash"]
    assert fidelity._canonical_json_hash(  # noqa: SLF001
        evidence["unexpected_revision_evidence"]
    ) == artifact["gap_generation"]["unexpected_revision_evidence_hash"]
    assert fidelity._canonical_json_hash(  # noqa: SLF001
        evidence["structural_error_evidence"]
    ) == artifact["gap_generation"]["structural_error_evidence_hash"]
    assert fidelity._canonical_json_hash(  # noqa: SLF001
        evidence["projection_reference_mismatch_evidence"]
    ) == artifact["gap_generation"]["projection_reference_mismatch_evidence_hash"]
    assert fidelity._canonical_json_hash(  # noqa: SLF001
        evidence["projection_metric_aggregate_mismatch_evidence"]
    ) == artifact["gap_generation"][
        "projection_metric_aggregate_mismatch_evidence_hash"
    ]
    gap_without_hash = dict(artifact["gap_generation"])
    gap_hash = gap_without_hash.pop("gap_hash")
    assert fidelity._canonical_json_hash(gap_without_hash) == gap_hash  # noqa: SLF001
    missing_keys = {
        "revision_id",
        "logical_event_id",
        "revision_number",
        "supersedes_revision_id",
        "content_hash",
        "full_content_hash",
        "projection_metadata_hash",
        "projection_metric_reference_hash",
        "projection_reference_hash",
        "visible_field_hashes",
    }
    unexpected_keys = {
        "revision_id",
        "observed_logical_event_id",
        "observed_visible_field_hashes",
        "logical_event_id",
        "revision_number",
        "supersedes_revision_id",
        "content_hash",
        "full_content_hash",
        "canonical_visible_field_hashes",
        "current_revision_id",
        "observed_projection_metadata_hash",
        "observed_projection_reference_hash",
        "projection_metadata_hash",
        "projection_metric_reference_hash",
        "projection_reference_hash",
    }
    visible_hash_fields = set(projection.VISIBLE_FIELDS)

    def assert_digest_map(value):
        assert isinstance(value, dict)
        assert set(value) == visible_hash_fields
        assert all(fidelity._is_hex_digest(item) for item in value.values())  # noqa: SLF001

    def assert_lineage_record(record, *, expected_keys):
        assert set(record) == expected_keys
        assert fidelity._is_revision_id(record["revision_id"])  # noqa: SLF001
        assert fidelity._is_logical_event_id(  # noqa: SLF001
            record["logical_event_id"]
        )
        assert type(record["revision_number"]) is int
        assert record["revision_number"] >= 0
        if record["revision_number"] == 0:
            assert record["supersedes_revision_id"] == ""
        else:
            assert fidelity._is_revision_id(  # noqa: SLF001
                record["supersedes_revision_id"]
            )
        assert fidelity._is_revision_content_digest(  # noqa: SLF001
            record["content_hash"]
        )
        assert (
            record["full_content_hash"] == ""
            or fidelity._is_revision_content_digest(  # noqa: SLF001
                record["full_content_hash"]
            )
        )
        assert fidelity._is_hex_digest(  # noqa: SLF001
            record["projection_metadata_hash"]
        )

    missing_records = evidence["missing_revision_evidence"]
    unexpected_records = evidence["unexpected_revision_evidence"]
    for record in missing_records:
        assert_lineage_record(record, expected_keys=missing_keys)
        assert fidelity._is_hex_digest(  # noqa: SLF001
            record["projection_reference_hash"]
        )
        assert fidelity._is_hex_digest(  # noqa: SLF001
            record["projection_metric_reference_hash"]
        )
        assert_digest_map(record["visible_field_hashes"])
    for record in unexpected_records:
        assert_lineage_record(record, expected_keys=unexpected_keys)
        assert fidelity._is_revision_id(  # noqa: SLF001
            record["current_revision_id"]
        )
        assert fidelity._is_logical_event_id(  # noqa: SLF001
            record["observed_logical_event_id"]
        )
        assert fidelity._is_hex_digest(  # noqa: SLF001
            record["observed_projection_metadata_hash"]
        )
        assert fidelity._is_hex_digest(  # noqa: SLF001
            record["observed_projection_reference_hash"]
        )
        assert_digest_map(record["observed_visible_field_hashes"])
        assert_digest_map(record["canonical_visible_field_hashes"])
    missing_ids = {record["revision_id"] for record in missing_records}
    assert len(missing_ids) == len(missing_records)
    assert len({record["revision_id"] for record in unexpected_records}) == 1
    historical = unexpected_records[0]
    assert historical["current_revision_id"] in missing_ids
    current = next(
        record
        for record in missing_records
        if record["revision_id"] == historical["current_revision_id"]
    )
    assert current["supersedes_revision_id"] == historical["revision_id"]
    assert current["revision_number"] == historical["revision_number"] + 1
    for record in evidence["structural_error_evidence"]:
        assert set(record) == {"error_code", "error_sha256"}
        assert isinstance(record["error_code"], str)
        assert record["error_code"].startswith("projection_")
        assert fidelity._is_hex_digest(record["error_sha256"])  # noqa: SLF001
    assert "snapshot_blob" not in decoded.decode("utf-8")


def test_v2_failure_report_is_complete_for_missing_and_unresolvable_db(
    tmp_path: Path,
):
    missing = audit_raw_projection_fidelity(
        raw_dir=tmp_path / "raw",
        db_path=tmp_path / "missing.db",
    )
    loop = tmp_path / "loop.db"
    loop.symlink_to(loop.name)
    unresolvable = audit_raw_projection_fidelity(
        raw_dir=tmp_path / "raw",
        db_path=loop,
    )

    required = {
        "schema_version",
        "ok",
        "raw_dir",
        "db_path",
        "expected_event_ids",
        "observed_event_ids",
        "missing_event_ids",
        "unexpected_event_ids",
        "duplicate_event_ids",
        "field_hash_mismatch_count",
        "logical_event_id_mismatch_count",
        "error_count",
        "errors",
        "gap_generation",
    }
    for report in (missing, unresolvable):
        assert required <= set(report)
        assert report["schema_version"] == fidelity.SCHEMA_VERSION
        assert report["ok"] is False
        assert report["error_count"] == 1
        assert report["gap_generation"]["classification"] == "audit_input_unreadable"


def test_gap_audit_rejects_wal_created_after_initial_inventory(
    tmp_path: Path,
    monkeypatch,
):
    raw_dir, db_path = _project(tmp_path)
    original = fidelity._parse_projection_file  # noqa: SLF001
    called = False

    def create_wal_during_projection(path: Path, **kwargs):
        nonlocal called
        if not called:
            called = True
            db_path.with_name(db_path.name + "-wal").write_bytes(b"concurrent-wal")
        return original(path, **kwargs)

    monkeypatch.setattr(fidelity, "_parse_projection_file", create_wal_during_projection)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert report["gap_generation"]["classification"] == "evidence_epoch_changed"


def test_superseded_revision_must_pair_with_missing_current_revision(tmp_path: Path):
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        for turn_number in (0, 1):
            store.upsert_turn(
                source_agent="opencode",
                session_id="pairing",
                turn_number=turn_number,
                user_content=f"original {turn_number}",
                assistant_content=f"assistant {turn_number}",
            )
        chunks = projection.build_projection_chunks(
            projection._fetch_refs(store),  # noqa: SLF001
            chunk_turns=5,
            max_chunks=None,
        )
        projection.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
        )
        excluded_current = store.upsert_turn(
            source_agent="opencode",
            session_id="pairing",
            turn_number=0,
            user_content="excluded replacement",
            assistant_content="assistant 0",
        )
        missing_current = store.upsert_turn(
            source_agent="opencode",
            session_id="pairing",
            turn_number=1,
            user_content="missing replacement",
            assistant_content="assistant 1",
        )
        excluded_logical_id = store.get_turn(excluded_current)["logical_event_id"]
        conn = store._pool.get_conn()  # noqa: SLF001
        ledger = NativeRawContractLedger()
        ledger.record_explicit(
            conn,
            logical_event_id=excluded_logical_id,
            revision_id=excluded_current,
            support_manifest_hash="test-native-contract-manifest",
            contract_state="nonconforming",
            contract_errors=["cross_session_native_identity"],
            observed_at="2026-07-25T00:00:00+00:00",
        )
        ledger.refresh_effective_state(
            conn,
            logical_event_id=excluded_logical_id,
            observed_at="2026-07-25T00:00:00+00:00",
        )
        conn.commit()
    finally:
        store.close()

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["missing_event_id_samples"] == [missing_current]
    assert report["gap_generation"]["paired_superseded_revision_count"] == 1
    assert report["gap_generation"]["unpaired_superseded_revision_count"] == 1
    assert (
        report["gap_generation"]["classification"]
        == "projection_content_or_structure_invalid"
    )


def test_unknown_unexpected_revision_is_never_certified_as_superseded(
    tmp_path: Path,
) -> None:
    raw_dir, db_path = _project(tmp_path)
    projection_file = next(raw_dir.rglob("*.md"))
    projected = projection_file.read_text(encoding="utf-8")
    original_revision_id = next(
        json.loads(line[len(projection.EVENT_MARKER_PREFIX) : -4])["event_id"]
        for line in projected.splitlines()
        if line.startswith(projection.EVENT_MARKER_PREFIX)
    )
    unknown_revision_id = "rawrev-" + "f" * 40
    assert original_revision_id != unknown_revision_id
    projection_file.write_text(
        projected.replace(original_revision_id, unknown_revision_id),
        encoding="utf-8",
    )
    relative_path = projection_file.relative_to(raw_dir).as_posix()

    def bind_unknown_revision(payload):
        metadata = payload["files"][relative_path]
        metadata["revision_ids"] = [unknown_revision_id]
        metadata["revision_set_hash"] = hashlib.sha256(
            json.dumps(
                metadata["revision_ids"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        metadata["content_hash"] = hashlib.sha256(
            projection_file.read_bytes()
        ).hexdigest()

    _rewrite_projection_journal(raw_dir, bind_unknown_revision)

    report = audit_raw_projection_fidelity(
        raw_dir=raw_dir,
        db_path=db_path,
        include_gap_evidence=True,
    )

    assert report["ok"] is False
    assert report["unexpected_event_ids"] == 1
    assert report["gap_generation"]["unknown_unexpected_revision_count"] == 1
    assert report["gap_generation"]["unexpected_superseded_revision_count"] == 0
    assert report["gap_generation"]["classification"] == (
        "projection_content_or_structure_invalid"
    )


# ---------------------------------------------------------------------------
# Paged projection (index + parts) fidelity tests
# ---------------------------------------------------------------------------


def _project_paged(
    tmp_path: Path,
    *,
    max_file_bytes: int = 3000,
    turn_count: int = 5,
) -> tuple[Path, Path]:
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        for turn_number in range(turn_count):
            assistant = f"paged assistant {turn_number}\n" + f"line-{turn_number:04d}\n" * 60
            if turn_number == 2:
                assistant = "oversized assistant\n" + "payload-%04d\n" * 500
            store.upsert_turn(
                source_agent="codex",
                session_id="paged-session",
                turn_number=turn_number,
                user_content=f"paged user {turn_number} " + "u" * 600,
                assistant_content=assistant,
                reasoning=f"paged reasoning {turn_number} " + "r" * 400,
                tool_calls=[{"name": "safe-tool", "arguments": {"x": turn_number}}],
                tool_results=[{"status": "ok", "turn_number": turn_number}],
                timestamp=f"2026-06-28T10:00:{turn_number:02d}",
            )
        chunks = projection.build_projection_chunks(
            projection._fetch_refs(store),  # noqa: SLF001
            chunk_turns=5,
            max_chunks=None,
        )
        projection.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=max_file_bytes,
        )
    finally:
        store.close()
    return raw_dir, db_path


def test_paged_projection_passes_fidelity_audit(tmp_path: Path):
    raw_dir, db_path = _project_paged(tmp_path)
    part_files = sorted(raw_dir.rglob("*.part-*.md"))
    assert part_files, "expected the chunk to be paged"
    assert any(
        b"mnemos-raw-field-cont-v2" in part_file.read_bytes() for part_file in part_files
    ), "expected at least one line-boundary field continuation"

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is True, report
    assert report["missing_event_ids"] == 0
    assert report["field_hash_mismatch_count"] == 0
    assert report["visible_fields_checked"] == 20


def test_paged_projection_audit_rejects_missing_part(tmp_path: Path):
    raw_dir, db_path = _project_paged(tmp_path)
    part_file = next(raw_dir.rglob("*.part-002.md"))
    part_file.unlink()

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert any("projection_part" in error for error in report["errors"])


def test_paged_projection_audit_rejects_tampered_part(tmp_path: Path):
    raw_dir, db_path = _project_paged(tmp_path)
    part_file = next(raw_dir.rglob("*.part-002.md"))
    data = part_file.read_bytes()
    part_file.write_bytes(data[:200] + b"X" + data[201:])

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert any("projection_part_hash_mismatch" in error for error in report["errors"])


def test_paged_projection_audit_rejects_orphan_part(tmp_path: Path):
    raw_dir, db_path = _project_paged(tmp_path)
    part_file = next(raw_dir.rglob("*.part-002.md"))
    orphan = part_file.with_name(part_file.name.replace(".part-002", ".part-099"))
    assert not orphan.exists()
    orphan.write_bytes(part_file.read_bytes())

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert "projection_journal_managed_path_set_mismatch" in report["errors"]

    def bind_orphan(payload):
        base_metadata = next(iter(payload["files"].values()))
        payload["files"][orphan.relative_to(raw_dir).as_posix()] = {
            "content_hash": hashlib.sha256(orphan.read_bytes()).hexdigest(),
            "logical_event_ids": base_metadata["logical_event_ids"],
            "revision_ids": base_metadata["revision_ids"],
            "revision_set_hash": base_metadata["revision_set_hash"],
        }

    _rewrite_projection_journal(raw_dir, bind_orphan)
    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert any("projection_part_orphan" in error for error in report["errors"])


def test_paged_projection_audit_rejects_tampered_index_page(tmp_path: Path):
    raw_dir, db_path = _project_paged(tmp_path)
    index_page = next(path for path in raw_dir.rglob("*.md") if ".part-" not in path.name)
    text = index_page.read_text(encoding="utf-8")
    index_page.write_text(text.replace("part_count:", "part_total:", 1), encoding="utf-8")
    _rebind_projection_file(raw_dir, index_page)

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is False
    assert any(
        "projection_index" in error or "projection_frontmatter" in error
        for error in report["errors"]
    )


def test_paged_projection_audit_passes_with_intra_line_field_split(tmp_path: Path):
    db_path = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw"
    store = RawEventStore(db_path=db_path, config=_Config(tmp_path))
    try:
        oversized_line = "中" * 3000 + "x" * 3000  # 12000 bytes, one line
        store.upsert_turn(
            source_agent="kimi",
            session_id="long-line-session",
            turn_number=0,
            user_content="short user",
            assistant_content=oversized_line,
            reasoning="short reasoning",
            tool_calls=[{"name": "safe-tool", "arguments": {"x": 0}}],
            tool_results=[{"status": "ok", "turn_number": 0}],
            timestamp="2026-05-21T10:00:00",
        )
        chunks = projection.build_projection_chunks(
            projection._fetch_refs(store),  # noqa: SLF001
            chunk_turns=5,
            max_chunks=None,
        )
        projection.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=0,
            max_file_bytes=2500,
        )
    finally:
        store.close()
    part_files = sorted(raw_dir.rglob("*.part-*.md"))
    assert part_files, "expected the single-line field to be paged"
    assert all(len(part_file.read_bytes()) <= 2500 for part_file in part_files)
    assert any(
        b"mnemos-raw-field-cont-v2" in part_file.read_bytes() for part_file in part_files
    )

    report = audit_raw_projection_fidelity(raw_dir=raw_dir, db_path=db_path)

    assert report["ok"] is True, report
    assert report["field_hash_mismatch_count"] == 0
    assert report["visible_fields_checked"] == 4
