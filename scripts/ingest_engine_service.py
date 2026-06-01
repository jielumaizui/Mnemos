#!/usr/bin/env python3

from __future__ import annotations
"""
【已废弃】Ingest Engine Service

旧的 Clean/Expand 处理服务已停止维护。
新体系使用 Karpathy 蒸馏范式：
  - Memos 原始素材无损保留，不再打 processed/ingest 标签
  - 每天晚上 8:30 由 distill_worker 自动蒸馏
  - 手动触发: python3 -m scripts.distill_worker --manual --uids uid1,uid2

保留此文件作为占位，避免调用方脚本报错。
"""

import sys


def main():
    print("=" * 60)
    print("【注意】ingest_engine_service 已废弃")
    print("=" * 60)
    print()
    print("新的知识蒸馏体系已启用：")
    print("  1. Memos 原始素材全部保留，状态由指纹表追踪")
    print("  2. 每天晚上 8:30 自动运行 distill_worker")
    print("  3. 手动触发: python3 -m scripts.distill_worker --manual --uids uid1,uid2")
    print()
    print("Wiki 产出目录: {wiki_dir}/")
    print("概念文: wiki/concepts/")
    print("话题串: wiki/threads/")
    print()
    print("如需帮助: python3 -m scripts.distill_worker --help")
    print("=" * 60)


if __name__ == "__main__":
    main()
