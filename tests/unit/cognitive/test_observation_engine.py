"""Unit tests for core.cognitive.observation_engine."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.cognitive.models import Dimension, Observation, ObservationType, SourceType
from core.cognitive.observation_engine import ObservationEngine, canonical_raw_engine_kwargs
from core.cognitive.observation_store import ObservationIndex, ObservationStore
from core.cognitive.sources import ContentSource, SourceItem, UserIntent


def test_canonical_raw_engine_kwargs_requires_database_dir(tmp_path):
    class RuntimeConfig:
        database_dir = tmp_path

    assert canonical_raw_engine_kwargs(RuntimeConfig()) == {
        "raw_events_db": str(tmp_path / "raw_events.db"),
        "require_canonical_raw": True,
    }
    with pytest.raises(TypeError, match="database_dir"):
        canonical_raw_engine_kwargs(object())


@pytest.fixture
def mock_extractor():
    """Return a fake dimension extractor that yields a deterministic observation."""

    class FakeExtractor:
        dimension = Dimension.ATTENTION

        def extract(self, items):
            yield Observation(
                id="obs-engine-001",
                dimension=Dimension.ATTENTION,
                observation_type=ObservationType.FREQUENCY,
                value={"concepts": {"ai": 3}, "total_mentions": 3, "dominant": "ai"},
                unit="mentions",
                confidence=0.8,
                source_type=SourceType.WIKI,
                source_path="/wiki/attention.md",
                source_id="session-1",
                evidence=["AI mentioned"],
                observed_at=datetime(2026, 6, 1, 10, 0, 0),
                period_start=datetime(2026, 6, 1, 0, 0, 0),
                period_end=datetime(2026, 6, 2, 0, 0, 0),
                content_source=ContentSource.NATIVE_DIALOGUE,
                user_intent_signal=UserIntent.UNKNOWN,
            )

    return FakeExtractor()


@pytest.fixture
def source_items():
    """Return synthetic SourceItems representing L1 and L2 sources."""
    return [
        SourceItem(
            source_type="raw",
            file_path="/raw/session-1.md",
            content="We discussed AI and coding today.",
            frontmatter={"session_id": "session-1", "date": "2026-06-01"},
            content_source=ContentSource.NATIVE_DIALOGUE,
            user_intent=UserIntent.SHARING_INFORMATION,
        ),
        SourceItem(
            source_type="wiki",
            file_path="/wiki/attention.md",
            content="AI is a dominant topic in recent sessions.",
            frontmatter={"session_id": "session-1", "date": "2026-06-02"},
            content_source=ContentSource.UNKNOWN,
            user_intent=UserIntent.UNKNOWN,
        ),
    ]


@pytest.fixture
def mock_reader(source_items):
    """Return a mocked SourceReader returning the synthetic source items."""
    reader = MagicMock()
    reader.read_all.return_value = iter(source_items)
    reader.read_since.return_value = iter(source_items)
    return reader


@pytest.fixture
def in_memory_store(tmp_path):
    """Return an in-memory ObservationStore backed by a temporary database."""
    return ObservationStore(db_path=str(tmp_path / "engine_test.db"))


@pytest.fixture
def engine(mock_reader, in_memory_store, mock_extractor):
    """Return an ObservationEngine with mocked reader, in-memory store, and fake extractor."""
    evidence_graph = MagicMock()
    engine = ObservationEngine(
        wiki_dir=None,
        store=in_memory_store,
        export_to_wiki=False,
        evidence_graph=evidence_graph,
    )
    engine.reader = mock_reader
    engine.extractors = [mock_extractor]
    return engine


class TestObservationEngineRun:
    """Tests for ObservationEngine.run behavior."""

    def test_run_returns_batch_with_extracted_observation(self, engine, source_items):
        batch = engine.run(persist=False)

        assert isinstance(batch, object)
        assert len(batch.observations) >= 1
        ids = [o.id for o in batch.observations]
        assert "obs-engine-001" in ids
        assert batch.source_count == len(source_items)
        assert "attention" in batch.dimension_counts

    def test_run_persists_to_store(self, engine, in_memory_store):
        _ = engine.run(persist=True)

        stored = in_memory_store.query()
        assert any(o.id == "obs-engine-001" for o in stored)
        assert len(stored) >= 1

    def test_system_signal_kinds_have_distinct_persistence_identities(
        self,
        engine,
        in_memory_store,
    ):
        engine.extractors = []

        batch = engine.run(persist=True)

        signals = {
            observation.source_id: observation
            for observation in in_memory_store.query()
            if observation.source_id.startswith("system:")
        }
        assert set(signals) == {
            "system:content_source_stats",
            "system:user_intent_stats",
        }
        assert "content_source_distribution" in signals[
            "system:content_source_stats"
        ].value
        assert "user_intent_distribution" in signals[
            "system:user_intent_stats"
        ].value
        assert len({observation.id for observation in signals.values()}) == 2
        assert len(batch.observations) == 2

    def test_run_records_evidence(self, engine):
        _ = engine.run(persist=True)

        # Evidence graph should be called at least once for the non-system observation.
        assert engine.evidence_graph.add_observation_sources.call_count >= 1
        call_kwargs = engine.evidence_graph.add_observation_sources.call_args.kwargs
        assert call_kwargs["observation_id"] == "obs-engine-001"
        assert len(call_kwargs["source_items"]) == 2

    def test_run_no_persist_does_not_write_store(self, engine, in_memory_store):
        engine.run(persist=False)
        assert len(in_memory_store.query()) == 0

    def test_run_incremental_uses_read_since(self, engine, mock_reader, source_items):
        since = datetime(2026, 6, 1)
        engine.run_incremental(since=since)

        mock_reader.read_since.assert_called_once_with(
            since,
            max_items=engine.MAX_INCREMENTAL_ITEMS,
            max_lookback_hours=engine.MAX_INCREMENTAL_LOOKBACK_HOURS,
        )

    def test_run_incremental_with_no_new_items(self, engine, mock_reader):
        mock_reader.read_since.return_value = iter([])
        since = datetime(2026, 6, 1)
        batch = engine.run_incremental(since=since)

        assert batch.observations == []
        assert batch.source_count == 0
        assert batch.extraction_status == "skipped"
        assert batch.extraction_reason == "no_new_source_items_since"

    def test_engine_uses_provided_index(self, tmp_path, mock_reader, mock_extractor):
        store = ObservationStore(db_path=str(tmp_path / "index_test.db"))
        index = ObservationIndex(store)
        evidence_graph = MagicMock()

        engine = ObservationEngine(
            wiki_dir=None,
            index=index,
            export_to_wiki=False,
            evidence_graph=evidence_graph,
        )
        engine.reader = mock_reader
        engine.extractors = [mock_extractor]

        engine.run(persist=True)
        stored = index.get_latest(limit=100)
        assert any(o.id == "obs-engine-001" for o in stored)
        assert len(stored) >= 1


class TestObservationEngineIncrementalReexport:
    """P108: 增量模式应避免无意义的全量 Wiki 重导出。"""

    def test_run_incremental_no_reexport_when_no_new_items(self, engine, mock_reader):
        mock_reader.read_since.return_value = iter([])
        engine.export_to_wiki = True
        engine.wiki_dir = "/tmp/wiki"
        engine._reexport_all = MagicMock()

        batch = engine.run_incremental(since=datetime(2026, 6, 1))

        assert batch.observations == []
        engine._reexport_all.assert_not_called()

    def test_run_incremental_no_reexport_when_no_changes(self, engine, mock_reader, source_items):
        # 先持久化一次，使后续 save_batch 返回 0 插入/更新（幂等）
        engine.run(persist=True)
        engine.export_to_wiki = True
        engine.wiki_dir = "/tmp/wiki"
        engine._reexport_all = MagicMock()
        # read_since 仍返回相同 items
        mock_reader.read_since.return_value = iter(source_items)

        batch = engine.run_incremental(since=datetime(2026, 6, 1), persist=True)

        assert len(batch.observations) >= 1
        engine._reexport_all.assert_not_called()

    def test_run_incremental_reexport_when_changed(self, engine, mock_reader, source_items):
        engine.export_to_wiki = True
        engine.wiki_dir = "/tmp/wiki"
        engine._reexport_all = MagicMock()
        mock_reader.read_since.return_value = iter(source_items)

        batch = engine.run_incremental(since=datetime(2026, 6, 1), persist=True)

        assert len(batch.observations) >= 1
        engine._reexport_all.assert_called_once()


class TestObservationEngineReexport:
    def test_reexport_all_only_changed_dimensions(self, engine, in_memory_store, tmp_path):
        """增量模式下应只重新导出发生变化的维度"""
        engine.wiki_dir = str(tmp_path / "wiki")
        engine.export_to_wiki = True

        # 先写入两个不同维度的 observation
        obs_attention = Observation(
            id="obs-attention",
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"concepts": {"ai": 1}},
            confidence=0.8,
            source_type=SourceType.WIKI,
            source_path="wiki/a.md",
        )
        obs_stress = Observation(
            id="obs-stress",
            dimension=Dimension.STRESS,
            observation_type=ObservationType.FREQUENCY,
            value={"level": "low"},
            confidence=0.8,
            source_type=SourceType.WIKI,
            source_path="wiki/b.md",
        )
        in_memory_store.save_batch([obs_attention, obs_stress])

        # 再保存一个 stress observation，触发增量导出
        engine.reader.read_since.return_value = iter([])
        engine.extractors = []
        # 直接调用 _reexport_all 只传 stress 维度
        engine._reexport_all(dimensions={"stress"})

        # attention 维度的文件不应被重新生成（但如果之前不存在也不会创建）
        # stress 维度的文件应存在
        assert (tmp_path / "wiki" / "L3-Observations" / "stress.md").exists()
