#!/usr/bin/env python3
"""
Observation Layer CLI — 命令行工具

用法：
    python3 -m core.cognitive.cli run         # 全量提取并存储
    python3 -m core.cognitive.cli stats       # 查看存储统计
    python3 -m core.cognitive.cli query [dim] # 查询观察
    python3 -m core.cognitive.cli clear       # 清空数据（谨慎）
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 确保 mnemos 在路径中
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.cognitive import Dimension, ObservationEngine, ObservationStore  # noqa: E402


def _parse_since(since_str: str) -> datetime:
    """解析 --since 参数"""
    from datetime import timedelta
    import re

    # 尝试 YYYY-MM-DD

    try:
        return datetime.strptime(since_str, "%Y-%m-%d")
    except ValueError:
        logging.getLogger(__name__).warning("[cli] ValueError suppressed", exc_info=True)

    # 尝试 Ndays / Nhours / Nhours
    m = re.match(r"(\d+)(d|days?|h|hours?)", since_str.lower())
    if m:
        num = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("d"):
            return datetime.now() - timedelta(days=num)
        elif unit.startswith("h"):
            return datetime.now() - timedelta(hours=num)

    raise ValueError(f"无法解析 --since 参数: {since_str}。支持格式: YYYY-MM-DD, Nd, Nhours")


def cmd_run(args):
    """执行观察提取"""
    print("=" * 60)
    print("Mnemos Observation Engine — V1 提取")
    print("=" * 60)

    engine = ObservationEngine(
        wiki_dir=args.wiki_dir,
        raw_events_db=getattr(args, "raw_events_db", None),
        require_canonical_raw=bool(getattr(args, "raw_events_db", None)),
    )

    # 先展示数据源统计
    stats = engine.reader.get_stats()
    print("\n📁 数据源:")
    print(f"   raw:  {stats['raw_events_db'] or '未配置'} ({stats['raw_items']} 条)")
    print(f"   wiki: {stats['wiki_dir'] or '未配置'} ({stats['wiki_files']} 文件)")
    print(f"   总计: {stats['total_items']} 项")

    if stats["total_items"] == 0:
        print("\n⚠️  没有找到任何数据源。请先配置 raw_events.db 或 wiki_dir。")
        return

    # 运行提取（全量或增量）
    if args.since:
        since = _parse_since(args.since)
        print(f"\n🔍 增量提取（since: {since.strftime('%Y-%m-%d %H:%M')}）...")
        batch = engine.run_incremental(since=since, persist=not args.dry_run)
    else:
        print("\n🔍 全量提取...")
        batch = engine.run(persist=not args.dry_run)

    print("\n✅ 提取完成:")
    print(f"   扫描文件: {batch.source_count}")
    print(f"   时间窗口: {batch.period_start.date()} ~ {batch.period_end.date()}")
    print(f"   观察总数: {batch.total_observations}")

    if batch.dimension_counts:
        print("\n📊 按维度分布:")
        for dim, count in sorted(batch.dimension_counts.items()):
            print(f"   {dim:15s}: {count} 条")

    # 展示每条观察
    if batch.observations:
        print("\n📋 观察详情:")
        for obs in batch.observations:
            print(f"\n   [{obs.dimension.value}] {obs.observation_type.value}")
            print(f"   值: {json.dumps(obs.value, ensure_ascii=False)[:200]}")
            if obs.unit:
                print(f"   单位: {obs.unit}")
            print(f"   置信度: {obs.confidence}")
            if obs.evidence:
                print(f"   证据: {obs.evidence[0][:80]}...")

    # 存储统计
    if not args.dry_run:
        store_stats = engine.get_store_stats()
        print("\n💾 存储统计:")
        print(f"   总观察数: {store_stats['total_observations']}")
        print(f"   最新更新: {store_stats['latest_update']}")


def cmd_stats(args):
    """查看存储统计"""
    store = ObservationStore(db_path=args.db)
    stats = store.get_stats()
    print(json.dumps(stats, indent=2, ensure_ascii=False))


def cmd_query(args):
    """查询观察"""
    store = ObservationStore(db_path=args.db)

    dim = None
    if args.dimension:
        try:
            dim = Dimension(args.dimension)
        except ValueError:
            print(f"无效维度: {args.dimension}")
            print(f"可用维度: {[d.value for d in Dimension]}")
            return

    results = store.query(dimension=dim, limit=args.limit)
    print(f"查询到 {len(results)} 条观察:\n")
    for obs in results:
        print(f"[{obs.dimension.value}] {obs.observation_type.value}")
        print(f"  值: {json.dumps(obs.value, ensure_ascii=False)[:300]}")
        print(f"  置信度: {obs.confidence} | 版本: v{obs.version}")
        print()


def cmd_clear(args):
    """清空数据"""
    if not args.force:
        confirm = input("⚠️  确定要清空所有 Observation 数据吗？输入 'yes' 确认: ")
        if confirm.strip().lower() != "yes":
            print("已取消")
            return

    store = ObservationStore(db_path=args.db)
    store.clear_all()
    print("✅ 已清空所有 Observation 数据")


def main():
    parser = argparse.ArgumentParser(description="Mnemos Observation Layer CLI")
    parser.add_argument(
        "--raw-events-db",
        help="canonical raw_events.db（提供后拒绝回退到 Markdown 解析）",
    )
    parser.add_argument("--wiki-dir", help="L2 wiki 仓库路径")
    parser.add_argument("--db", help="数据库路径（默认 ~/.mnemos/observations.db）")

    sub = parser.add_subparsers(dest="command")

    # run
    run_parser = sub.add_parser("run", help="执行观察提取")
    run_parser.add_argument("--dry-run", action="store_true", help="只提取，不存储")
    run_parser.add_argument("--since", help="增量提取，格式: YYYY-MM-DD 或 N(days|hours)")

    # stats
    sub.add_parser("stats", help="查看存储统计")

    # query
    query_parser = sub.add_parser("query", help="查询观察")
    query_parser.add_argument("dimension", nargs="?", help="维度名")
    query_parser.add_argument("--limit", type=int, default=20)

    # clear
    clear_parser = sub.add_parser("clear", help="清空数据")
    clear_parser.add_argument("--force", action="store_true", help="跳过确认")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "query":
        cmd_query(args)
    elif args.command == "clear":
        cmd_clear(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
