import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.hephaestus.distillation_engine import DistillationEngine
from core.hephaestus.distillation_models import DistillationResult, KnowledgeFragment
from core.hephaestus.raw_provenance import attach_raw_provenance, record_page_provenance
from core.sync_framework.capture_handoff import CaptureHandoffStore
from core.sync_framework.capture_worker import CaptureWorkerPool
from core.sync_framework.raw_event_store import RawEventStore
from tests.cognition_episode_fixtures import bind_admitted_cognition_episode


def test_raw_turn_content_changes_append_immutable_revisions(tmp_path):
    store = RawEventStore(db_path=tmp_path / "raw_events.db")
    try:
        first = store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=7,
            user_content="first immutable bytes",
            assistant_content="answer one",
            completeness={"visible_text": "full"},
        )
        second = store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=7,
            user_content="second immutable bytes",
            assistant_content="answer two",
            completeness={"visible_text": "full"},
        )
        duplicate = store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=7,
            user_content="second immutable bytes",
            assistant_content="answer two",
            completeness={"visible_text": "full"},
        )

        assert first != second
        assert duplicate == second
        assert store.get_turn(first)["user_content"] == "first immutable bytes"
        assert store.get_turn(second)["user_content"] == "second immutable bytes"
        assert store.find_event_id(
            source_agent="codex", session_id="session-1", turn_number=7
        ) == second
        revisions = store.list_revisions(
            source_agent="codex", session_id="session-1", turn_number=7
        )
        assert [row["revision_id"] for row in revisions] == [first, second]
        assert revisions[1]["supersedes_revision_id"] == first
        assert revisions[0]["content_hash"] != revisions[1]["content_hash"]
    finally:
        store.close()


def test_capture_handoff_carries_revision_and_full_turn_span(tmp_path):
    conn = sqlite3.connect(tmp_path / "capture_queue.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE capture_events (
            id INTEGER PRIMARY KEY,
            status TEXT,
            processed_at TEXT,
            error TEXT
        )
        """
    )
    conn.execute("INSERT INTO capture_events(id, status) VALUES (1, 'processing')")
    CaptureHandoffStore.ensure_schema(conn)
    events = [
        {
                "id": 1,
                "turn_number": 3,
                "turn_id": "turn-3",
                "content_hash": "hash-3",
                "raw_revision_id": "rawrev-immutable",
                "payload": {
                    "user_content": "abc",
                    "assistant_content": "defgh",
                    "metadata": {
                        "raw_event_id": "rawrev-immutable",
                        "logical_event_id": "raw-codex-session-3",
                        "raw_content_hash": "hash-3",
                        "cognitive_sync_event_ids": ["cde-sync-1", "cde-sync-2"],
                        },
                    },
        }
    ]

    handoff = CaptureHandoffStore.create(
        conn,
        source_agent="codex",
        session_id="session-1",
        events=events,
    )

    assert handoff["meta"]["raw_event_refs"] == [
        {
            "revision_id": "rawrev-immutable",
            "logical_event_id": "raw-codex-session-3",
            "turn_number": 3,
            "content_hash": "hash-3",
            "span_start": 0,
            "span_end": 8,
        }
    ]
    # Keep the existing full-turn metadata entry unchanged, while exposing
    # precise role-local spans on the actual extractor inputs.
    assert handoff["messages"] == [
        {
            "role": "user",
            "content": "abc",
            "turn": 3,
            "turn_number": 3,
            "source_span": {
                "revision_id": "rawrev-immutable",
                "logical_event_id": "raw-codex-session-3",
                "turn_number": 3,
                "content_hash": "hash-3",
                "role": "user",
                "span_start": 0,
                "span_end": 3,
            },
        },
        {
            "role": "assistant",
            "content": "defgh",
            "turn": 3,
            "turn_number": 3,
            "source_span": {
                "revision_id": "rawrev-immutable",
                "logical_event_id": "raw-codex-session-3",
                "turn_number": 3,
                "content_hash": "hash-3",
                "role": "assistant",
                "span_start": 3,
                "span_end": 8,
            },
        },
    ]
    assert handoff["meta"]["cognitive_sync_event_ids"] == ["cde-sync-1", "cde-sync-2"]


def test_provenance_edges_are_revision_and_span_addressed(tmp_path):
    store = RawEventStore(db_path=tmp_path / "raw_events.db")
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="session-1",
            turn_number=1,
            user_content="0123456789",
            assistant_content="abcdefghij",
        )
        edge_id = store.record_provenance_edge(
            source_revision_id=revision_id,
            span_start=2,
            span_end=9,
            consumer_type="distill_chunk",
            consumer_id="task-1:chunk-0",
        )
        duplicate = store.record_provenance_edge(
            source_revision_id=revision_id,
            span_start=2,
            span_end=9,
            consumer_type="distill_chunk",
            consumer_id="task-1:chunk-0",
        )

        assert duplicate == edge_id
        assert store.list_provenance_edges(revision_id) == [
            {
                "edge_id": edge_id,
                "source_revision_id": revision_id,
                "span_start": 2,
                "span_end": 9,
                "consumer_type": "distill_chunk",
                "consumer_id": "task-1:chunk-0",
            }
        ]
        assert store.get_metrics(revision_id)["reference_count"] == 1
    finally:
        store.close()


def test_provenance_metric_failure_rolls_back_the_edge_and_releases_transaction(tmp_path):
    db_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=db_path)
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="transaction-failure",
            turn_number=1,
            user_content="0123456789",
            assistant_content="abcdefghij",
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TRIGGER abort_reference_metric
                BEFORE UPDATE OF reference_count ON raw_metrics
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic metric failure');
                END
                """
            )

        with pytest.raises(sqlite3.IntegrityError, match="synthetic metric failure"):
            store.record_provenance_edge(
                source_revision_id=revision_id,
                span_start=2,
                span_end=9,
                consumer_type="distill_chunk",
                consumer_id="task-rollback:chunk-0",
            )

        assert store.list_provenance_edges(revision_id) == []
        assert store.get_metrics(revision_id)["reference_count"] == 0
        with sqlite3.connect(db_path) as conn:
            assert conn.in_transaction is False
    finally:
        store.close()


def test_distillation_propagates_refs_and_records_amphora_and_page_edges(monkeypatch):
    ref = {
        "revision_id": "rawrev-immutable",
        "logical_event_id": "raw-logical",
        "turn_number": 3,
        "content_hash": "hash-3",
        "span_start": 0,
        "span_end": 8,
    }
    result = DistillationResult(session_id="session-1", raw_event_refs=[ref])
    fragment = KnowledgeFragment(
        form="pattern",
        title="immutable provenance test",
        frontmatter={},
        background="background",
        core_content="## Evidence\n" + ("evidence " * 20),
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    attach_raw_provenance(result, [fragment])
    assert fragment.frontmatter["raw_event_refs"] == [ref]

    class _FakeStore:
        def __init__(self):
            self.edges = []

        def record_provenance_edge(self, **edge):
            self.edges.append(edge)

        def resolve_provenance_gaps(self, **_identity):
            return 0

        def close(self):
            return None

    fake = _FakeStore()
    monkeypatch.setattr(
        "core.sync_framework.raw_event_store.RawEventStore",
        lambda *_args, **_kwargs: fake,
    )
    CaptureWorkerPool._record_handoff_provenance(
        {"raw_event_refs": [ref]}, "amphora-task-1"
    )
    record_page_provenance(result, ("/tmp/wiki-page.md",))

    assert [edge["consumer_type"] for edge in fake.edges] == [
        "amphora_task",
        "wiki_page",
    ]
    assert all(edge["source_revision_id"] == ref["revision_id"] for edge in fake.edges)
    assert all((edge["span_start"], edge["span_end"]) == (0, 8) for edge in fake.edges)


def test_chunked_page_provenance_uses_persisted_fragment_mapping(monkeypatch, tmp_path):
    """Each chunked output page must retain only its own exact raw span."""
    from core.ops.cognitive_pipeline_receipts import persist_distillation_with_receipt

    session_refs = [
        {"revision_id": "raw-a", "span_start": 0, "span_end": 8},
        {"revision_id": "raw-b", "span_start": 0, "span_end": 8},
    ]
    first = KnowledgeFragment(
        form="pattern",
        title="first exact chunk provenance",
        frontmatter={
            "raw_event_refs": [
                {"revision_id": "raw-a", "span_start": 1, "span_end": 3}
            ]
        },
        background="background",
        core_content="## Evidence\n" + ("evidence " * 20),
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    second = KnowledgeFragment(
        form="pattern",
        title="second exact chunk provenance",
        frontmatter={
            "raw_event_refs": [
                {"revision_id": "raw-b", "span_start": 4, "span_end": 7}
            ]
        },
        background="background",
        core_content="## Evidence\n" + ("evidence " * 20),
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    first_path = tmp_path / "first.md"
    second_path = tmp_path / "second.md"

    class _FakeStore:
        def __init__(self):
            self.edges = []

        def record_provenance_edge(self, **edge):
            self.edges.append(edge)

        def resolve_provenance_gaps(self, **_identity):
            return 0

        def close(self):
            return None

    class _Config:
        database_dir = tmp_path / ".cognitive"

        @staticmethod
        def get(key, default=None):
            if key == "distill.action_router.enabled":
                return True
            return default

    class _Engine:
        _runtime_receipt_config = object()

        @staticmethod
        def _validate_structured_output_contract(*_args):
            return True

        @staticmethod
        def _prepare_fragments(fragments, _cfg):
            return fragments

        @staticmethod
        def _filter_accepted_fragments(_result, fragments, _cfg):
            return fragments

        @staticmethod
        def _persist_pages(_result, _fragments):
            return [str(first_path), str(second_path)], [
                (Path(first_path), first),
                (Path(second_path), second),
            ]

        @staticmethod
        def _route_structured_actions(result, fragments, _cfg):
            return _Engine._persist_pages(result, fragments)

        @staticmethod
        def _link_cross_agent(_file_fragments):
            return None

        @staticmethod
        def _write_metrics_back(_file_fragments):
            return None

        @staticmethod
        def _emit_distill_events(_result, _file_fragments, _written):
            return None

    fake = _FakeStore()
    monkeypatch.setattr(
        "core.sync_framework.raw_event_store.RawEventStore",
        lambda *_args, **_kwargs: fake,
    )
    monkeypatch.setattr(
        "core.ops.cognitive_pipeline_receipts.record_distillation_write_receipt",
        lambda *_args, **_kwargs: None,
    )
    result = DistillationResult(
        session_id="chunked-page-provenance",
        judgment="knowledge",
        fragments=[first, second],
        raw_event_refs=session_refs,
        # Any nonempty bundle marks this as a chunked output.  A lost mapping
        # must therefore not fall back to the whole session's two refs.
        chunk_extraction_results=[object()],
    )
    bind_admitted_cognition_episode(
        result,
        _Config.database_dir,
        source_event_ids=("raw-a", "raw-b"),
    )

    receipt = persist_distillation_with_receipt(_Engine(), result, _Config())

    assert receipt.status == "committed"
    assert {
        (
            edge["source_revision_id"],
            edge["span_start"],
            edge["span_end"],
            edge["consumer_id"],
        )
        for edge in fake.edges
    } == {
        ("raw-a", 1, 3, str(first_path.resolve())),
        ("raw-b", 4, 7, str(second_path.resolve())),
    }


def test_chunked_routed_update_uses_explicit_page_refs_without_retry_gap(
    monkeypatch, tmp_path
):
    from core.ops.cognitive_pipeline_receipts import persist_distillation_with_receipt

    page = tmp_path / "routed-update.md"
    ref = {
        "revision_id": "raw-update",
        "content_hash": "sha256:update",
        "span_start": 2,
        "span_end": 9,
    }
    fragment = KnowledgeFragment(
        form="pattern",
        title="chunked routed update exact provenance",
        frontmatter={},
        background="background",
        core_content="## Evidence\n" + ("evidence " * 20),
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )

    class _FakeStore:
        def __init__(self):
            self.edges = []
            self.gaps = []

        def record_provenance_edge(self, **edge):
            self.edges.append(edge)

        def record_provenance_gap(self, **gap):
            self.gaps.append(gap)

        def resolve_provenance_gaps(self, **_identity):
            return 0

        def close(self):
            return None

    class _Config:
        database_dir = tmp_path / ".cognitive"

        @staticmethod
        def get(key, default=None):
            if key == "distill.action_router.enabled":
                return True
            return default

    class _Engine:
        _runtime_receipt_config = object()

        @staticmethod
        def _validate_structured_output_contract(*_args):
            return True

        @staticmethod
        def _prepare_fragments(fragments, _cfg):
            return fragments

        @staticmethod
        def _filter_accepted_fragments(_result, fragments, _cfg):
            return fragments

        @staticmethod
        def _route_structured_actions(result, _fragments, _cfg):
            page.write_text("# routed update\n", encoding="utf-8")
            result.page_raw_event_refs = [(page, (ref,))]
            return [str(page)], []

        @staticmethod
        def _link_cross_agent(_file_fragments):
            return None

        @staticmethod
        def _write_metrics_back(_file_fragments):
            return None

        @staticmethod
        def _emit_distill_events(_result, _file_fragments, _written):
            return None

    fake = _FakeStore()
    monkeypatch.setattr(
        "core.sync_framework.raw_event_store.RawEventStore",
        lambda *_args, **_kwargs: fake,
    )
    monkeypatch.setattr(
        "core.ops.cognitive_pipeline_receipts.record_distillation_write_receipt",
        lambda *_args, **_kwargs: None,
    )
    result = DistillationResult(
        session_id="chunked-routed-update",
        source="codex",
        judgment="knowledge",
        fragments=[fragment],
        structured_output={"claims": []},
        chunk_extraction_results=[object()],
    )
    bind_admitted_cognition_episode(
        result,
        _Config.database_dir,
        source_event_ids=("raw-update",),
    )

    receipt = persist_distillation_with_receipt(_Engine(), result, _Config())

    assert receipt.status == "committed"
    assert fake.gaps == []
    assert fake.edges == [
        {
            "source_revision_id": "raw-update",
            "span_start": 2,
            "span_end": 9,
            "consumer_type": "wiki_page",
            "consumer_id": str(page.resolve()),
        }
    ]


def test_chunked_provenance_never_falls_back_to_full_session_refs():
    result = DistillationResult(
        session_id="chunked-unprovenanced-page",
        analysis_type="chunked",
        distill_input_mode="chunked",
        raw_event_refs=[{"revision_id": "raw-session", "span_start": 0, "span_end": 8}],
    )
    fragment = KnowledgeFragment(
        form="pattern",
        title="chunked fragment without exact span",
        frontmatter={},
        background="background",
        core_content="## Evidence\n" + ("evidence " * 20),
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )

    attach_raw_provenance(result, [fragment])

    assert "raw_event_refs" not in fragment.frontmatter


def test_chunked_page_with_malformed_fragment_refs_is_retryable(monkeypatch, tmp_path):
    from core.ops.cognitive_pipeline_receipts import persist_distillation_with_receipt

    page = tmp_path / "malformed-chunk-page.md"
    fragment = KnowledgeFragment(
        form="pattern",
        title="chunked fragment with malformed exact span",
        frontmatter={
            "raw_event_refs": [
                {"revision_id": "raw-a", "span_start": 4, "span_end": 4}
            ]
        },
        background="background",
        core_content="## Evidence\n" + ("evidence " * 20),
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )

    class _FakeStore:
        def __init__(self):
            self.edges = []
            self.gaps = []
            self.resolved = []

        def record_provenance_edge(self, **edge):
            self.edges.append(edge)

        def record_provenance_gap(self, **gap):
            self.gaps.append(gap)

        def resolve_provenance_gaps(self, **identity):
            self.resolved.append(identity)
            return 0

        def close(self):
            return None

    class _Config:
        database_dir = tmp_path / ".cognitive"

        @staticmethod
        def get(key, default=None):
            if key == "distill.action_router.enabled":
                return True
            return default

    post_write_hooks = []
    engine = SimpleNamespace(
        _runtime_receipt_config=object(),
        _validate_structured_output_contract=lambda *_args: True,
        _prepare_fragments=lambda fragments, _cfg: fragments,
        _filter_accepted_fragments=lambda _result, fragments, _cfg: fragments,
        _persist_pages=lambda _result, _fragments: (
            [str(page)],
            [(Path(page), fragment)],
        ),
        _route_structured_actions=lambda result, fragments, _cfg: (
            [str(page)],
            [(Path(page), fragment)],
        ),
        _link_cross_agent=lambda _fragments: post_write_hooks.append("link"),
        _write_metrics_back=lambda _fragments: post_write_hooks.append("metrics"),
        _emit_distill_events=lambda *_args: post_write_hooks.append("events"),
    )
    fake = _FakeStore()
    monkeypatch.setattr(
        "core.sync_framework.raw_event_store.RawEventStore",
        lambda *_args, **_kwargs: fake,
    )
    monkeypatch.setattr(
        "core.ops.cognitive_pipeline_receipts.record_distillation_write_receipt",
        lambda *_args, **_kwargs: None,
    )
    result = DistillationResult(
        session_id="chunked-malformed-page",
        source="codex",
        judgment="knowledge",
        fragments=[fragment],
        raw_event_refs=[{"revision_id": "raw-a", "span_start": 0, "span_end": 8}],
        chunk_extraction_results=[object()],
    )
    bind_admitted_cognition_episode(
        result,
        _Config.database_dir,
        source_event_ids=("raw-a",),
    )

    receipt = persist_distillation_with_receipt(engine, result, _Config())

    assert receipt.status == "retryable_failed"
    assert receipt.terminal_reason == "chunked_page_provenance_missing"
    assert receipt.written_pages == (str(page),)
    assert receipt.failed_count == 1
    assert receipt.terminal is False
    assert fake.edges == []
    assert fake.resolved == []
    assert fake.gaps == [
        {
            "consumer_type": "wiki_page",
            "consumer_id": str(page.resolve()),
            "reason": "chunked_fragment_raw_provenance_missing",
            "source_agent": "codex",
            "session_id": "chunked-malformed-page",
        }
    ]
    assert post_write_hooks == []


def test_distillation_rejects_invalid_raw_span_before_pipeline_work():
    engine = object.__new__(DistillationEngine)
    result = engine.process(
        "session-1",
        [{"role": "user", "content": "must not reach the LLM"}],
        meta={
            "raw_event_refs": [
                {"revision_id": "rawrev-invalid", "span_start": 4, "span_end": 4}
            ]
        },
    )
    assert result.judgment == "error"
    assert result.judgment_reason == "invalid immutable raw provenance metadata"


def test_metadata_search_headers_do_not_depend_on_metrics_row(tmp_path):
    store = RawEventStore(db_path=tmp_path / "raw_events.db")
    try:
        revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="legacy-no-metrics",
            turn_number=0,
            user_content="legacy",
            assistant_content="canonical",
        )
        logical_id = store.get_turn(revision_id)["logical_event_id"]
        conn = store._pool.get_conn()  # noqa: SLF001 - deliberate legacy corruption fixture
        conn.execute("DELETE FROM raw_metrics WHERE event_id=?", (logical_id,))
        conn.commit()

        headers = store.list_current_headers(session_id="legacy-no-metrics")
        assert [item["revision_id"] for item in headers] == [revision_id]
        assert headers[0]["retention_state"] == "active"
    finally:
        store.close()
