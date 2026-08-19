"""
SyncEngine smoke benchmarks.

These tests exercise the real SyncEngine pipeline against a temporary SQLite
database and an in-memory StorageBackend.  They deliberately avoid Obsidian,
Memos, network calls, and checked-in benchmark databases so they can run in the
default test suite.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Dict, List

from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn
from core.sync_framework.storage_backend import StorageBackend, StorageResult
from core.sync_framework.sync_engine import SyncEngine


class _BenchmarkConfig:
    storage_backend = "obsidian"

    def __init__(self, tmp_path: Path):
        self.data_dir = tmp_path / "data"
        self.database_dir = tmp_path / "db"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str, default=None):
        values = {
            "storage.obsidian.daily_size_threshold": 819200,
            "capture.reasoning_mode": "artifact_summary",
        }
        return values.get(key, default)


class _MemoryStorageBackend(StorageBackend):
    def __init__(self):
        self.records: Dict[str, StorageResult] = {}
        self._next_id = 0

    def save(self, content: str, tags: List[str], title: str) -> List[StorageResult]:
        self._next_id += 1
        uid = f"bench-{self._next_id:05d}"
        result = StorageResult(
            uid=uid,
            content=content,
            tags=list(tags),
            metadata={"title": title},
        )
        self.records[uid] = result
        return [result]

    def search(self, query: str, limit: int | None = None) -> List[StorageResult]:
        matches = [r for r in self.records.values() if query in r.content]
        return matches[:limit] if limit is not None else matches

    def list_by_tags(self, tags: List[str], limit: int | None = None) -> List[StorageResult]:
        required = set(tags)
        matches = [r for r in self.records.values() if required.issubset(set(r.tags))]
        return matches[:limit] if limit is not None else matches

    def get_by_id(self, uid: str) -> StorageResult | None:
        return self.records.get(uid)

    def health_check(self) -> Dict[str, str]:
        return {"status": "ok", "message": "in-memory benchmark backend"}

    def update_tags(
        self,
        uid: str,
        add_tags: List[str] | None = None,
        remove_tags: List[str] | None = None,
    ) -> StorageResult | None:
        result = self.records.get(uid)
        if result is None:
            return None
        tags = set(result.tags)
        tags.update(add_tags or [])
        tags.difference_update(remove_tags or [])
        result.tags = sorted(tags)
        return result


class _BenchmarkSource(AgentSource):
    def __init__(self):
        self._turns_by_path: Dict[Path, List[Turn]] = {}

    @property
    def name(self) -> str:
        # Exercise the real native-source contract with a declared source.
        # The benchmark model tag/content remain synthetic and hermetic.
        return "codex"

    @property
    def model_tag(self) -> str:
        return "benchmark-model"

    def discover_sessions(self) -> List[SessionInfo]:
        return [
            SessionInfo(session_id=path.stem, source_path=path)
            for path in self._turns_by_path
        ]

    def parse_turns(self, session_path: Path) -> List[Turn]:
        return self._turns_by_path[session_path]

    def add_session(self, tmp_path: Path, session_id: str, turn_count: int) -> SessionInfo:
        source_path = tmp_path / f"{session_id}.jsonl"
        turns = [
            Turn(
                turn_number=i,
                user_content=f"How should benchmark session {session_id} handle item {i}?",
                assistant_content=(
                    f"Benchmark answer {i} for {session_id}. "
                    "This content is intentionally small but not empty."
                ),
            )
            for i in range(turn_count)
        ]
        self._turns_by_path[source_path] = turns
        return SessionInfo(session_id=session_id, source_path=source_path)


def _new_engine(tmp_path: Path, monkeypatch) -> SyncEngine:
    from core.sync_framework import sync_engine

    cfg = _BenchmarkConfig(tmp_path)
    monkeypatch.setattr(sync_engine, "get_config", lambda: cfg)
    return SyncEngine(
        backend=_MemoryStorageBackend(),
        db_path=str(cfg.database_dir / "sync_log.db"),
    )


def _p95_ms(latencies_ms: List[float]) -> float:
    ordered = sorted(latencies_ms)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def test_sync_session_smoke_latency(tmp_path: Path, monkeypatch):
    engine = _new_engine(tmp_path, monkeypatch)
    source = _BenchmarkSource()
    try:
        # Warm database schema and imports outside the measured loop.
        warm = source.add_session(tmp_path, "warmup", 1)
        engine.sync_session(source, warm, incremental=False)

        latencies_ms: List[float] = []
        for i in range(12):
            session = source.add_session(tmp_path, f"session-{i}", 5)
            start = time.perf_counter()
            results = engine.sync_session(source, session, incremental=False)
            latencies_ms.append((time.perf_counter() - start) * 1000)
            assert [r.action for r in results] == ["new"] * 5

        p95 = _p95_ms(latencies_ms)
        mean = sum(latencies_ms) / len(latencies_ms)
        print(f"sync_session 5-turn smoke: mean={mean:.1f}ms p95={p95:.1f}ms")
        assert p95 < 500, f"sync_session P95 {p95:.1f}ms exceeded 500ms smoke budget"
    finally:
        engine.close()


def test_sync_batch_smoke_throughput(tmp_path: Path, monkeypatch):
    engine = _new_engine(tmp_path, monkeypatch)
    source = _BenchmarkSource()
    try:
        sessions = [source.add_session(tmp_path, f"batch-{i}", 3) for i in range(10)]
        start = time.perf_counter()
        result = engine.sync_batch(source, sessions, incremental=False)
        elapsed_ms = (time.perf_counter() - start) * 1000

        print(f"sync_batch 10x3 smoke: elapsed={elapsed_ms:.1f}ms")
        assert result.total_sessions == 10
        assert len(result.successful) == 10
        assert not result.failed
        assert result.turn_stats["new"] == 30
        assert elapsed_ms < 3000, f"sync_batch {elapsed_ms:.1f}ms exceeded 3s smoke budget"
    finally:
        engine.close()
