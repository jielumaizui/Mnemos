"""Metrics command for Mnemos CLI."""

import logging
from pathlib import Path

from core.cli.helpers import _get_config

logger = logging.getLogger(__name__)


def cmd_metrics_scan(args):
    """扫描 Wiki 页面 metrics"""
    try:
        from core.wiki_metrics import WikiMetrics

        wiki_dir = _get_config().wiki_dir
        wm = WikiMetrics(wiki_dir=str(wiki_dir))
        print(f"扫描 Wiki metrics: {wiki_dir}")
        result = wm.scan_all_pages()
        print(f"  扫描完成: {result['total']} 个页面")
        print(f"  新增: {result['inserted']}  更新: {result['updated']}")
        if result.get("deleted", 0):
            print(f"  清理失效 metrics: {result['deleted']}")
        try:
            from core.wiki_metrics import write_mnemos_home

            home = write_mnemos_home(str(wiki_dir))
            print(f"  已更新首页: {home}")
        except (ImportError, AttributeError, OSError) as e:
            print(f"  首页更新失败: {e}")
    except (ImportError, AttributeError, OSError) as e:
        print(f"扫描失败: {e}")


def cmd_metrics_assess(args):
    """快速评估单个 Wiki 页面并写入 metrics。"""
    try:
        from core.wiki_metrics import quick_assess

        wiki_dir = _get_config().wiki_dir
        page_path = Path(args.page).expanduser()
        if not page_path.is_absolute():
            page_path = wiki_dir / page_path
        if not page_path.exists():
            print(f"评估失败: 页面不存在 {page_path}")
            return

        content = page_path.read_text(encoding="utf-8")
        try:
            metric_path = str(page_path.relative_to(wiki_dir))
        except ValueError:
            metric_path = str(page_path)

        result = quick_assess(
            metric_path,
            content,
            source_count=max(0, int(getattr(args, "source_count", 1))),
        )
        print(f"评估页面: {metric_path}")
        print(f"  质量分: {result['quality_score']}")
        print(f"  知识阶段: {result['stage']}")
        print(f"  证据等级: {result['evidence_level']}")
    except (ImportError, AttributeError, OSError, ValueError) as e:
        print(f"评估失败: {e}")
