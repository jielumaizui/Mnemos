"""
Observation Index — CLI 调试入口

用法：
    python3 -m core.cognitive.observation_index --rebuild
    python3 -m core.cognitive.observation_index --stats
    python3 -m core.cognitive.observation_index --check

数据源：canonical raw_events.db 与 Wiki。
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.cognitive.observation_store import ObservationIndex
from core.config import get_config

# Constants extracted from magic numbers
ALL_OBS_LIMIT = 10000

logger = logging.getLogger(__name__)


def _resolve_sources(args) -> tuple[str, Optional[str]]:
    """解析 canonical Raw 数据库与 Wiki 目录。"""
    config = get_config()
    wiki_dir = args.wiki_dir or Path(config.wiki_dir).expanduser()
    raw_events_db = getattr(args, "raw_events_db", None) or (
        Path(config.database_dir).expanduser() / "raw_events.db"
    )

    wiki_dir = str(wiki_dir) if wiki_dir and Path(wiki_dir).exists() else None
    return str(Path(raw_events_db).expanduser()), wiki_dir


def cmd_rebuild(args) -> int:
    """从 L1/L2 重新构建 Observation Index"""
    raw_events_db, wiki_dir = _resolve_sources(args)
    if not wiki_dir:
        print("错误：未找到 Wiki 目录。请设置 MNEMOS_WIKI_DIR 或 --wiki-dir。", file=sys.stderr)
        return 1

    index = ObservationIndex()
    print("开始重建 Observation Index...")
    print(f"  raw_events_db: {raw_events_db}")
    print(f"  wiki_dir: {wiki_dir}")

    result = index.rebuild_from_sources(
        raw_events_db=raw_events_db,
        wiki_dir=wiki_dir,
        backup=not args.no_backup,
    )

    print("\n重建完成：")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args) -> int:
    """打印 Observation Index 统计"""
    index = ObservationIndex()
    stats = index.get_stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_check(args) -> int:
    """运行 Observation Index 完整性检查"""
    _raw_events_db, wiki_dir = _resolve_sources(args)
    checker = ObservationIndexIntegrityCheck(
        index=ObservationIndex(),
        wiki_dir=wiki_dir,
    )
    report = checker.run()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["healthy"] else 1


class ObservationIndexIntegrityCheck:
    """
    Observation Index 完整性检查

    检查项：
    1. Index 非空（若源目录存在）
    2. 每条 Observation 有非空 source_id
    3. 每条 Observation 置信度在 [0, 1] 区间
    4. Wiki 投影目录与 Index 维度数量一致（若 wiki_dir 存在）
    """

    def __init__(
        self,
        index: Optional[ObservationIndex] = None,
        wiki_dir: Optional[str] = None,
    ):
        self.index = index or ObservationIndex()
        self.wiki_dir = wiki_dir

    def run(self) -> dict:
        """执行检查并返回报告"""
        issues = []
        all_obs = self.index.query(limit=ALL_OBS_LIMIT)
        stats = self.index.get_stats()

        # 检查 1：Index 非空
        source_exists = bool(self.wiki_dir and Path(self.wiki_dir).exists())
        if source_exists and stats["total_observations"] == 0:
            issues.append("源目录存在但 Observation Index 为空")

        # 检查 2 & 3：字段有效性
        for obs in all_obs:
            if not obs.source_id and not obs.source_path:
                issues.append(f"Observation {obs.id} 缺少 source_id 与 source_path，无法追溯")
            if not (0.0 <= obs.confidence <= 1.0):
                issues.append(f"Observation {obs.id} 置信度越界: {obs.confidence}")

        # 检查 4：Wiki 投影一致性
        projection_ok = True
        if self.wiki_dir:
            obs_dir = Path(self.wiki_dir) / "L3-Observations"
            if obs_dir.exists():
                # 分页投影的 <dim>.part-NNN.md 分片归属于 <dim>.md 索引页，
                # 不是独立维度文件。
                wiki_files = {
                    p.stem
                    for p in obs_dir.glob("*.md")
                    if ".part-" not in p.stem
                }
                index_dims = set(stats["by_dimension"].keys())
                missing_in_wiki = index_dims - wiki_files
                missing_in_index = wiki_files - index_dims
                if missing_in_wiki:
                    issues.append(f"Index 维度未在 Wiki 投影: {missing_in_wiki}")
                    projection_ok = False
                if missing_in_index:
                    issues.append(f"Wiki 投影包含未知维度: {missing_in_index}")
                    projection_ok = False

        return {
            "healthy": len(issues) == 0,
            "checked_at": datetime.now().isoformat(),
            "total_observations": stats["total_observations"],
            "issues": issues,
            "projection_ok": projection_ok if self.wiki_dir else None,
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Observation Index 调试工具",
    )
    parser.add_argument(
        "--wiki-dir",
        type=str,
        default=None,
        help="Wiki 目录路径",
    )
    parser.add_argument(
        "--raw-events-db",
        type=str,
        default=None,
        help="canonical raw_events.db 路径",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    rebuild_parser = subparsers.add_parser("rebuild", help="从 L1/L2 重建 Index")
    rebuild_parser.add_argument(
        "--no-backup",
        action="store_true",
        help="重建前不备份数据库",
    )
    rebuild_parser.set_defaults(func=cmd_rebuild)

    stats_parser = subparsers.add_parser("stats", help="查看 Index 统计")
    stats_parser.set_defaults(func=cmd_stats)

    check_parser = subparsers.add_parser("check", help="运行完整性检查")
    check_parser.set_defaults(func=cmd_check)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    return args.func(args)  # type: ignore[no-any-return]


if __name__ == "__main__":
    sys.exit(main())
