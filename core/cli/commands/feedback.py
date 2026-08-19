"""Feedback command for Mnemos CLI."""

import json
import logging

from core.config import get_config as _get_config  # noqa: F401

logger = logging.getLogger(__name__)


def cmd_feedback(args):
    """Feedback（L5）CLI 入口。"""
    from core.reflection.reflection_engine import ReflectionEngine

    engine = ReflectionEngine()
    if args.feedback_cmd == "stats":
        summary = engine.get_feedback_summary(days=args.days)
        print(f"最近 {args.days} 天反馈统计：")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("用法: mnemos feedback stats [--days N]")
