#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端导入测试脚本
处理用户指定的 4 个文件，观察蒸馏效果
"""

import sys
import logging
from pathlib import Path

# 确保 mnemos 在路径中
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_ingest")

from core.kia.knowledge_inbox import KnowledgeInbox, InboxFile


def main():
    inbox = KnowledgeInbox()

    files = [
        Path.home() / "Desktop" / "2026年6月营销计划宣贯.pdf",
        Path.home() / "Desktop" / "常州事业部到家业务数据分析报告_2026年5月.html",
        Path.home() / "Desktop" / "到家" / "ai" / "书" / "增长黑客 ([美]肖恩·埃利斯,[美]摩根·布朗) (z-library.sk, 1lib.sk, z-lib.sk).epub",
        Path.home() / "Desktop" / "到家" / "ai" / "2025年数据" / "2025年锡常事业部6月数据汇总.xlsx",
    ]

    for fpath in files:
        if not fpath.exists():
            logger.error(f"文件不存在: {fpath}")
            continue

        logger.info(f"\n{'='*60}")
        logger.info(f"处理文件: {fpath.name}")
        logger.info(f"大小: {fpath.stat().st_size / 1024:.1f} KB")
        logger.info(f"{'='*60}")

        inbox_file = InboxFile(
            path=fpath,
            filename=fpath.name,
            size=fpath.stat().st_size,
            mtime=fpath.stat().st_mtime,
            hash="",
            status="pending",
        )

        try:
            result = inbox.process_file(inbox_file)
            logger.info(f"结果: {result}")
        except Exception as e:
            logger.exception(f"处理失败: {e}")


if __name__ == "__main__":
    main()
