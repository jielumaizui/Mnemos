"""COG-027 contract tests for canonical Raw-backed observations.

These tests intentionally exercise the public observation path rather than a
fixture-only parser.  A current Raw revision must be the source of truth,
every Raw item must reach a typed terminal result, and the readiness metric
must reject legacy ``source_id`` self-claims that lack exact provenance.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from core.cognitive.models import Dimension, Observation, ObservationType, SourceType
from core.cognitive.observation_engine import ObservationEngine
from core.cognitive.observation_store import ObservationStore
from core.cognitive.sources import CanonicalRawUnavailable, SourceReader, UserIntent
from core.ops.cognitive_readiness_lineage import observation_lineage_metric
from core.sync_framework.raw_event_store import (
    RawEventStore,
    iter_current_raw_turns_readonly,
)


class _SingleRawEvidenceExtractor:
    """Emit one deterministic observation for one canonical Raw input."""

    dimension = Dimension.ATTENTION

    def extract(self, items):
        for item in items:
            if item.source_type != "raw" or not item.raw_revision_id:
                continue
            yield Observation(
                dimension=Dimension.ATTENTION,
                observation_type=ObservationType.FREQUENCY,
                value={"canonical_raw": item.raw_revision_id},
                unit="items",
                confidence=1.0,
                source_type=SourceType.RAW,
                source_path=item.file_path,
                source_id=item.raw_revision_id,
                evidence=["canonical Raw evidence"],
            )


class _DuplicateRawEvidenceExtractor:
    """Emit two same-key candidates to exercise store-level batch dedupe."""

    dimension = Dimension.ATTENTION

    def extract(self, items):
        for item in items:
            if item.source_type != "raw" or not item.raw_revision_id:
                continue
            for value in ("discarded", "persisted"):
                yield Observation(
                    dimension=Dimension.ATTENTION,
                    observation_type=ObservationType.FREQUENCY,
                    value={"candidate": value},
                    unit="items",
                    confidence=1.0,
                    source_type=SourceType.RAW,
                    source_path=item.file_path,
                    source_id=item.raw_revision_id,
                    evidence=["canonical Raw evidence"],
                )


def _append_raw_turn(store: RawEventStore, *, turn_number: int, text: str) -> str:
    return store.upsert_turn(
        source_agent="codex",
        session_id="cog-027-session",
        turn_number=turn_number,
        user_content=text,
        assistant_content="acknowledged",
        timestamp=f"2026-07-14T00:00:0{turn_number}+00:00",
        completeness={"visible_text": "full"},
    )


def test_canonical_raw_reader_uses_current_revision_and_keeps_wiki_fair(tmp_path):
    raw_db = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_db)
    try:
        first = _append_raw_turn(store, turn_number=1, text="first raw revision")
        current = _append_raw_turn(store, turn_number=1, text="current raw revision")
        _append_raw_turn(store, turn_number=2, text="another raw turn")
        assert first != current

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "user-note.md").write_text(
            "---\ndate: 2026-07-14\nsource: user\n---\n\nA user-owned wiki note.",
            encoding="utf-8",
        )

        reader = SourceReader(
            raw_events_db=str(raw_db),
            wiki_dir=str(wiki),
            require_canonical_raw=True,
        )
        items = list(reader.read_since(max_items=2))

        raw_items = [item for item in items if item.source_type == "raw"]
        wiki_items = [item for item in items if item.source_type == "wiki"]
        assert len(raw_items) == 1
        assert len(wiki_items) == 1
        assert raw_items[0].raw_revision_id == current
        assert raw_items[0].raw_event_id
        assert raw_items[0].content == "current raw revision"
        assert raw_items[0].assistant_content == "acknowledged"
    finally:
        store.close()


def test_canonical_raw_reader_rejects_incomplete_identity_contract_without_markdown_fallback(
    tmp_path,
):
    """A pre-contract DB must not be quietly reinterpreted as Raw Markdown."""
    raw_db = tmp_path / "raw_events.db"
    with sqlite3.connect(raw_db) as conn:
        conn.executescript(
            """
            CREATE TABLE raw_turns (
                event_id TEXT PRIMARY KEY,
                current_revision_id TEXT,
                source_agent TEXT,
                session_id TEXT,
                conversation_at TEXT,
                captured_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE raw_turn_revisions (
                revision_id TEXT PRIMARY KEY,
                logical_event_id TEXT,
                content_hash TEXT,
                snapshot_blob BLOB
            );
            CREATE TABLE raw_metrics (event_id TEXT PRIMARY KEY, retention_state TEXT);
            """
        )
    raw_dir = tmp_path / "raw-projection"
    raw_dir.mkdir()
    (raw_dir / "projection.md").write_text("**User**: fallback must not run", encoding="utf-8")

    reader = SourceReader(
        raw_projection_dir=str(raw_dir),
        raw_events_db=str(raw_db),
        require_canonical_raw=True,
    )

    with pytest.raises(CanonicalRawUnavailable, match="identity alias schema is missing"):
        list(reader.read_all())


def test_observation_run_records_exact_raw_provenance_after_persist(tmp_path):
    raw_db = tmp_path / "raw_events.db"
    observation_db = tmp_path / "observations.db"
    store = RawEventStore(db_path=raw_db)
    try:
        revision_id = _append_raw_turn(store, turn_number=1, text="raw evidence for observation")
        observation_store = ObservationStore(db_path=str(observation_db))
        engine = ObservationEngine(
            raw_events_db=str(raw_db),
            store=observation_store,
            export_to_wiki=False,
        )
        engine.extractors = [_SingleRawEvidenceExtractor()]

        batch = engine.run(persist=True)

        assert len(batch.observations) == 1
        observation = observation_store.get_by_id(batch.observations[0].id)
        assert observation is not None
        assert observation.source_id == revision_id
        assert store.list_provenance_edges(revision_id) == [
            {
                "edge_id": store.list_provenance_edges(revision_id)[0]["edge_id"],
                "source_revision_id": revision_id,
                "span_start": 0,
                "span_end": len("raw evidence for observation"),
                "consumer_type": "observation",
                "consumer_id": observation.id,
            }
        ]
    finally:
        store.close()


def test_raw_provenance_uses_only_the_same_key_observation_that_was_persisted(tmp_path):
    """Discarded batch candidates must never receive durable Raw edges."""
    raw_db = tmp_path / "raw_events.db"
    observation_db = tmp_path / "observations.db"
    store = RawEventStore(db_path=raw_db)
    try:
        revision_id = _append_raw_turn(store, turn_number=1, text="dedupe raw evidence")
        observation_store = ObservationStore(db_path=str(observation_db))
        engine = ObservationEngine(
            raw_events_db=str(raw_db),
            store=observation_store,
            export_to_wiki=False,
        )
        engine.extractors = [_DuplicateRawEvidenceExtractor()]
        engine.evidence_graph = None

        batch = engine.run(persist=True)

        assert len(batch.observations) == 2
        persisted = observation_store.query(source_type=SourceType.RAW)
        assert len(persisted) == 1
        assert persisted[0].source_id == revision_id
        assert persisted[0].value == {"candidate": "persisted"}
        edges = store.list_provenance_edges(revision_id)
        assert len(edges) == 1
        assert edges[0]["consumer_id"] == persisted[0].id
        metric = observation_lineage_metric(
            raw_db,
            observation_db,
            freshness_window_seconds=3600,
            now=datetime.now(timezone.utc),
        )
        assert metric["coverage_ratio"] == 1.0
        assert metric["invalid_edge_count"] == 0
    finally:
        store.close()


def test_empty_current_raw_gets_typed_intentional_no_observation_terminal(tmp_path):
    raw_db = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_db)
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="cog-027-empty",
            turn_number=1,
            user_content="",
            assistant_content="",
            completeness={"visible_text": "full"},
        )
        engine = ObservationEngine(
            raw_events_db=str(raw_db),
            store=ObservationStore(db_path=str(tmp_path / "observations.db")),
            export_to_wiki=False,
        )
        engine.extractors = []

        batch = engine.run(persist=True)

        assert batch.observations == []
        with sqlite3.connect(raw_db) as conn:
            receipt = conn.execute(
                """
                SELECT consumer_id, reason, status
                FROM raw_provenance_gaps
                WHERE consumer_type='observation' AND consumer_id=?
                """,
                (revision_id,),
            ).fetchone()
        assert receipt == (revision_id, "empty_visible_content", "intentional_no_observation")
    finally:
        store.close()


def test_readiness_rejects_legacy_logical_event_source_id_without_exact_edge(tmp_path):
    raw_db = tmp_path / "raw_events.db"
    observation_db = tmp_path / "observations.db"
    store = RawEventStore(db_path=raw_db)
    try:
        revision_id = _append_raw_turn(store, turn_number=1, text="legacy source id must not pass")
        logical_event_id = store.get_turn(revision_id)["logical_event_id"]
        observation_store = ObservationStore(db_path=str(observation_db))
        observation_store.save(
            Observation(
                dimension=Dimension.ATTENTION,
                observation_type=ObservationType.FREQUENCY,
                value={"legacy": True},
                source_type=SourceType.RAW,
                source_id=logical_event_id,
            )
        )

        metric = observation_lineage_metric(
            raw_db,
            observation_db,
            freshness_window_seconds=3600,
            now=datetime.now(timezone.utc),
        )

        assert metric["denominator"] == 1
        assert metric["covered"] == 0
        assert metric["uncovered"] == 1
    finally:
        store.close()


def test_current_v2_projection_is_hash_verified_and_matches_db(tmp_path):
    """The retained Markdown reader must be parity-checked, never regex-only."""
    from scripts import project_raw_vault as projection

    raw_db = tmp_path / "raw_events.db"
    raw_dir = tmp_path / "raw-vault"
    store = RawEventStore(db_path=raw_db)
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="compat-session",
            turn_number=1,
            user_content="请你分析一下当前迁移方案是否可行？",
            assistant_content="我会按证据逐项验证。",
            reasoning="private reasoning is still projection-visible in v2",
            tool_calls=[{"name": "inspect", "arguments": {"target": "Raw"}}],
            tool_results=[{"status": "ok"}],
            attachments=[{"asset_id": "a1"}],
            timestamp="2026-07-14T01:00:00+00:00",
            completeness={"visible_text": "full"},
        )
        chunks = projection.build_projection_chunks(
            projection._fetch_refs(store),  # noqa: SLF001 - projection contract fixture
            chunk_turns=5,
            max_chunks=None,
        )
        projection.write_projection(
            raw_dir,
            store,
            chunks,
            db_path=raw_db,
            max_turn_chars=0,
        )

        reader = SourceReader(
            raw_projection_dir=str(raw_dir),
            raw_events_db=str(raw_db),
            require_canonical_raw=True,
        )
        direct = list(reader.read_all())
        verified_projection = list(reader.read_verified_raw_projection())

        assert [item.raw_revision_id for item in direct] == [revision_id]
        assert [item.raw_revision_id for item in verified_projection] == [revision_id]
        assert verified_projection[0].content == direct[0].content
        assert verified_projection[0].raw_event_id == direct[0].raw_event_id
        assert verified_projection[0].session_id == "compat-session"
        assert verified_projection[0].source_agent == "codex"
        assert direct[0].user_intent == UserIntent.SEEKING_JUDGMENT
    finally:
        store.close()


def test_canonical_raw_keeps_long_user_content_and_missing_assistant(tmp_path):
    raw_db = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_db)
    try:
        user_content = "请分析 " + ("x" * 120_000)
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="long-and-missing-assistant",
            turn_number=1,
            user_content=user_content,
            assistant_content="",
            completeness={"visible_text": "full"},
        )
        item = list(SourceReader(raw_events_db=str(raw_db)).read_all())[0]

        assert item.raw_revision_id == revision_id
        assert item.user_content == user_content
        assert item.assistant_content == ""
        assert item.content == user_content
        assert item.user_intent == UserIntent.SEEKING_JUDGMENT
    finally:
        store.close()


def test_observation_raw_page_does_not_retain_structured_payload(tmp_path):
    """Observation needs visible text, not every historical tool-result blob."""
    raw_db = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_db)
    try:
        store.upsert_turn(
            source_agent="codex",
            session_id="visible-only",
            turn_number=1,
            user_content="保留可见正文",
            assistant_content="保留可见回复",
            tool_calls=[{"name": "inspect"}],
            tool_results=[{"payload": "large-structured-result"}],
            reasoning="private structured field",
            completeness={"visible_text": "full"},
        )

        turn = next(
            iter_current_raw_turns_readonly(
                raw_db,
                include_structured_payload=False,
            )
        )

        assert turn.user_content == "保留可见正文"
        assert turn.assistant_content == "保留可见回复"
        assert turn.reasoning == ""
        assert turn.tool_calls == []
        assert turn.tool_results == []
    finally:
        store.close()


def test_nonempty_raw_without_supported_signal_gets_typed_terminal(tmp_path):
    raw_db = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_db)
    try:
        revision_id = _append_raw_turn(store, turn_number=1, text="unsupported but visible")
        engine = ObservationEngine(
            raw_events_db=str(raw_db),
            store=ObservationStore(db_path=str(tmp_path / "observations.db")),
            export_to_wiki=False,
        )
        engine.extractors = []

        engine.run(persist=True)

        with sqlite3.connect(raw_db) as conn:
            receipt = conn.execute(
                """
                SELECT reason, status FROM raw_provenance_gaps
                WHERE consumer_type='observation' AND consumer_id=?
                """,
                (revision_id,),
            ).fetchone()
        assert receipt == ("no_supported_signal", "intentional_no_observation")
        metric = observation_lineage_metric(
            raw_db,
            tmp_path / "observations.db",
            freshness_window_seconds=3600,
            now=datetime.now(timezone.utc),
        )
        assert metric["coverage_ratio"] == 1.0
        assert metric["observation_created"] == 0
        assert metric["all_visible_raw_skipped"] == 1
    finally:
        store.close()


def test_extractor_failure_keeps_canonical_raw_retryable_and_cursor_unadvanced(tmp_path):
    class _FailingExtractor:
        dimension = Dimension.ATTENTION

        def extract(self, _items):
            raise RuntimeError("synthetic extractor failure")

    raw_db = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_db)
    try:
        revision_id = _append_raw_turn(store, turn_number=1, text="must remain retryable")
        observation_store = ObservationStore(db_path=str(tmp_path / "observations.db"))
        engine = ObservationEngine(
            raw_events_db=str(raw_db),
            store=observation_store,
            export_to_wiki=False,
        )
        engine.extractors = [_FailingExtractor()]

        with pytest.raises(RuntimeError, match="synthetic extractor failure"):
            engine.run_incremental(datetime(1970, 1, 1, tzinfo=timezone.utc), persist=True)

        with sqlite3.connect(raw_db) as conn:
            terminal_count = conn.execute(
                """
                SELECT COUNT(*) FROM raw_provenance_gaps
                WHERE consumer_type='observation' AND consumer_id=?
                """,
                (revision_id,),
            ).fetchone()[0]
            edge_count = conn.execute(
                "SELECT COUNT(*) FROM raw_provenance_edges WHERE source_revision_id=?",
                (revision_id,),
            ).fetchone()[0]
        assert terminal_count == 0
        assert edge_count == 0
        assert observation_store.get_source_cursors() == {}
    finally:
        store.close()


def test_incremental_cursor_reaches_more_than_one_thousand_raw_items_without_wiki_starvation(
    tmp_path,
):
    raw_db = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_db)
    try:
        revisions = [
            _append_raw_turn(store, turn_number=index, text=f"raw-{index}")
            for index in range(1, 1002)
        ]
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "note.md").write_text(
            "---\nsource: user\ndate: 2026-07-14\n---\n\nA fair-source wiki note.",
            encoding="utf-8",
        )
        observation_store = ObservationStore(db_path=str(tmp_path / "observations.db"))
        engine = ObservationEngine(
            raw_events_db=str(raw_db),
            wiki_dir=str(wiki),
            store=observation_store,
            export_to_wiki=False,
        )
        engine.extractors = [_SingleRawEvidenceExtractor()]
        engine.evidence_graph = None
        engine._calibrate_and_log = lambda _batch, _items: {}  # type: ignore[method-assign]

        first = engine.run_incremental(datetime(1970, 1, 1, tzinfo=timezone.utc))
        cursors_after_first = observation_store.get_source_cursors()
        second = engine.run_incremental(datetime(1970, 1, 1, tzinfo=timezone.utc))

        assert first.source_count == 1000
        assert second.source_count == 2
        assert sum(1 for obs in first.observations if obs.source_type == SourceType.RAW) == 999
        assert sum(1 for obs in second.observations if obs.source_type == SourceType.RAW) == 2
        assert "wiki" in cursors_after_first
        assert "canonical_raw" in cursors_after_first
        assert observation_store.get_source_cursors()["canonical_raw"]["revision_id"] in revisions
        with sqlite3.connect(raw_db) as conn:
            terminal_edges = conn.execute(
                """
                SELECT COUNT(*) FROM raw_provenance_edges
                WHERE consumer_type='observation'
                """
            ).fetchone()[0]
        assert terminal_edges == len(revisions)
    finally:
        store.close()


def test_full_canonical_replay_is_paged_and_reports_the_exact_total(tmp_path):
    """The formal full path must not materialize a 1,001-item Raw backlog."""
    raw_db = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_db)
    try:
        revisions = [
            _append_raw_turn(store, turn_number=index, text=f"full-replay-{index}")
            for index in range(1, 1002)
        ]
        observation_store = ObservationStore(db_path=str(tmp_path / "observations.db"))
        engine = ObservationEngine(
            raw_events_db=str(raw_db),
            store=observation_store,
            export_to_wiki=False,
        )
        engine.extractors = [_SingleRawEvidenceExtractor()]
        engine.evidence_graph = None
        engine._calibrate_and_log = lambda _batch, _items: {}  # type: ignore[method-assign]

        batch = engine.run(persist=True)

        assert batch.total_observations == len(revisions)
        assert batch.observations_truncated is True
        assert len(batch.observations) == 1000
        assert observation_store.get_source_cursors()["canonical_raw"]["revision_id"] in revisions
        metric = observation_lineage_metric(
            raw_db,
            tmp_path / "observations.db",
            freshness_window_seconds=3600,
            now=datetime.now(timezone.utc),
        )
        assert metric["denominator"] == len(revisions)
        assert metric["covered"] == len(revisions)
        assert metric["uncovered"] == 0
    finally:
        store.close()


def test_bounded_raw_iterator_yields_an_oversized_revision_without_losing_cursor_progress(tmp_path):
    """A byte page cap may split pages, but it must never skip lossless Raw."""
    raw_db = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=raw_db)
    try:
        expected = [
            _append_raw_turn(store, turn_number=index, text=f"bounded-{index}" * 800)
            for index in range(1, 4)
        ]
        seen = []
        cursor = {}
        while True:
            page = list(
                iter_current_raw_turns_readonly(
                    raw_db,
                    cursor=cursor,
                    max_snapshot_bytes=1,
                )
            )
            if not page:
                break
            assert len(page) == 1
            seen.extend(page)
            cursor = page[-1].cursor_token
        assert {turn.revision_id for turn in seen} == set(expected)
        assert len(seen) == len(expected)
    finally:
        store.close()
