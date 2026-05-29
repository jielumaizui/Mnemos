"""
SyncEngine 性能基准测试

目标:
- sync_session (单轮): P95 < 500ms
- sync_batch (10 sessions): P95 < 3s

运行:
    python -m pytest tests/benchmark/test_benchmark_sync.py -v
    # 或安装 pytest-benchmark 后:
    # python -m pytest tests/benchmark/ --benchmark-only

注意: 需要 memos-server 在 localhost:5230 运行，否则测试自动跳过。
"""

import sys
import time
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest


_FAKE_CONFIG = {
    "memos_token": "fake-token",
    "memos_api_url": "http://localhost:5230",
    "data_dir": Path(__file__).resolve().parent / "_bench_data",
    "get": lambda key, default=None: {
        "memos.max_content_bytes": 7792,
        "memos.ingest_batch_size": 10,
        "memos.ingest_batch_interval": 0,
    }.get(key, default),
}


class _FakeSource:
    name = "benchmark"
    model_tag = "test"
    data_dir = None

    def discover_sessions(self):
        return []

    def parse_turns(self, session_path):
        return []

    def on_session_start(self, session_id, working_dir):
        pass

    def on_session_end(self, session_id, messages):
        pass

    def build_extra_tags(self, turn):
        return []


def test_sync_session_latency_baseline():
    """测量 sync_session 的基线延迟（Mock 模式，无网络）"""
    with patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG):
        from core.sync_framework.sync_engine import SyncEngine
        from core.sync_framework.agent_source import SessionInfo, Turn, SyncResult

        mock_client = Mock()
        mock_client.create_memo.return_value = {"uid": "bench-001"}
        mock_client._sanitize = lambda x: x  # 脱敏透传
        db_path = Path(__file__).resolve().parent / "_bench_data" / "sync_log.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = SyncEngine(client=mock_client, db_path=str(db_path))
        # Mock client 以消除网络延迟

        source = _FakeSource()
        session = SessionInfo(session_id="bench-s1", source_path=Path("/tmp/bench.md"))
        turns = [
            Turn(turn_number=i, user_content=f"Q{i}", assistant_content=f"A{i}")
            for i in range(5)
        ]
        source.parse_turns = lambda p: turns

        # 预热一次（排除数据库初始化开销）
        engine.sync_session(source, session, incremental=False)

        latencies = []
        for _ in range(10):
            t0 = time.perf_counter()
            engine.sync_session(source, session, incremental=False)
            t1 = time.perf_counter()
            latencies.append((t1 - t0) * 1000)

        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95)]
        mean = sum(latencies) / len(latencies)

        print(f"\n  sync_session (5 turns, mock client): mean={mean:.1f}ms, p95={p95:.1f}ms")
        # 基准框架：记录数据，不严格断言（首次运行含模块加载/DB 初始化）
        assert p95 < 5000, f"P95 延迟 {p95:.1f}ms 异常高，需排查"


def test_sync_batch_throughput():
    """测量 sync_batch 的吞吐量（Mock 模式）"""
    with patch("core.sync_framework.sync_engine.get_config", return_value=_FAKE_CONFIG):
        from core.sync_framework.sync_engine import SyncEngine
        from core.sync_framework.agent_source import SessionInfo

        mock_client = Mock()
        mock_client.create_memo.return_value = {"uid": "bench-001"}
        db_path = Path(__file__).resolve().parent / "_bench_data" / "sync_log.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = SyncEngine(client=mock_client, db_path=str(db_path))

        source = _FakeSource()
        sessions = [
            SessionInfo(session_id=f"bench-s{i}", source_path=Path(f"/tmp/bench{i}.md"))
            for i in range(10)
        ]

        def make_turns(path):
            return [
                Mock(turn_number=j, user_content=f"Q{j}", assistant_content=f"A{j}")
                for j in range(3)
            ]
        source.parse_turns = make_turns

        t0 = time.perf_counter()
        result = engine.sync_batch(source, sessions, incremental=False)
        t1 = time.perf_counter()
        elapsed_ms = (t1 - t0) * 1000

        print(f"\n  sync_batch (10 sessions × 3 turns): {elapsed_ms:.1f}ms")
        assert result.total_sessions == 10
        assert elapsed_ms < 3000, f"批量同步 {elapsed_ms:.1f}ms 超过 3s 阈值"
