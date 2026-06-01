#!/usr/bin/env python3

from __future__ import annotations
"""
【已废弃】batch_clean_submit

旧的 Clean 提交脚本已停止维护。
新体系使用 Karpathy 蒸馏范式：
  - Memos 原始素材无损保留
  - 每天晚上 8:30 由 distill_worker 自动蒸馏
  - 用户可随时手动触发: python3 -m scripts.distill_worker --manual --uids abc,def

保留此文件作为占位，避免调用方脚本报错。
运行时会打印提示信息。
"""

import sys

def batch_submit_to_clean(limit: int = 50):
    print("=" * 60)
    print("【注意】batch_clean_submit 已废弃")
    print("=" * 60)
    print()
    print("新的知识蒸馏体系已启用：")
    print("  1. Memos 原始素材全部保留，不再打 processed/ingest 标签")
    print("  2. 每天晚上 8:30 自动运行 distill_worker，把对话提炼成 Wiki")
    print("  3. 手动触发: python3 -m scripts.distill_worker --manual --uids uid1,uid2")
    print()
    print("Wiki 产出目录: {wiki_dir}/")
    print("概念文: wiki/concepts/")
    print("话题串: wiki/threads/")
    print()
    print("如需帮助，查看: python3 -m scripts.distill_worker --help")
    print("=" * 60)
    return 0, 0


if __name__ == "__main__":
    batch_submit_to_clean()
