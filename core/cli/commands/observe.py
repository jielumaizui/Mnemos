"""Observe command for Mnemos CLI."""

import logging

from core.cli.helpers import _get_config

logger = logging.getLogger(__name__)


def cmd_observe(args):
    """Observation（L3）CLI 入口。"""
    if args.observe_cmd == "run":
        from core.cognitive.observation_engine import (
            ObservationEngine,
            canonical_raw_engine_kwargs,
        )

        cfg = _get_config()
        engine = ObservationEngine(
            wiki_dir=str(cfg.wiki_dir),
            **canonical_raw_engine_kwargs(cfg),
        )
        if args.full:
            batch = engine.run(persist=True)
        elif args.since:
            from datetime import datetime

            since_dt = datetime.fromisoformat(args.since)
            batch = engine.run_incremental(since=since_dt, persist=True)
        else:
            batch = engine.run(persist=True)

        print(
            f"Observation 提取完成: {batch.total_observations} 条观察，{len(batch.dimension_counts)} 个维度"
        )
        for dim, count in batch.dimension_counts.items():
            print(f"  - {dim}: {count}")

    elif args.observe_cmd == "search":
        from core.cognitive.observation_store import ObservationIndex
        from core.cognitive.models import Dimension, SourceType

        index = ObservationIndex()
        dim = Dimension(args.dimension) if args.dimension else None
        st = SourceType(args.source_type) if args.source_type else None
        if dim and st is None:
            observations = index.get_by_dimension(dim, limit=args.limit)
        else:
            observations = index.query(dimension=dim, source_type=st, limit=args.limit)
        print(f"找到 {len(observations)} 条观察：")
        for obs in observations:
            summary = obs.evidence[0] if obs.evidence else str(obs.value)[:80]
            print(f"  [{obs.dimension.value}] {obs.observation_type.value}: {summary}")

    elif args.observe_cmd == "stats":
        from core.cognitive.observation_store import ObservationIndex

        stats = ObservationIndex().get_stats()
        print("Observation Index 统计：")
        print(f"  总观察数: {stats.get('total_observations', 0)}")
        print(f"  按维度: {stats.get('by_dimension', {})}")
        print(f"  按来源: {stats.get('by_source', {})}")
        print(f"  最近更新: {stats.get('latest_update', 'N/A')}")

    else:
        print("用法: mnemos observe {run|search|stats}")
