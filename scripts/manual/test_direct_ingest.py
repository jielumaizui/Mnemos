#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直接文档导入测试 — 不走 Memos，直接蒸馏入 Wiki
"""

import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_direct_ingest")

from core.hephaestus.document_processor import DocumentProcessor


def main():
    processor = DocumentProcessor()

    files = [
        Path.home() / "Desktop" / "常州事业部到家业务数据分析报告_2026年5月.html",
        Path.home() / "Desktop" / "2025年锡常事业部6月数据汇总.xlsx",
        Path.home() / "Desktop" / "2026年6月营销计划宣贯.pdf",
        Path.home() / "Desktop" / "到家" / "ai" / "书" / "增长黑客 ([美]肖恩·埃利斯,[美]摩根·布朗) (z-library.sk, 1lib.sk, z-lib.sk).epub",
    ]

    for fpath in files:
        if not fpath.exists():
            logger.error(f"文件不存在: {fpath}")
            continue

        logger.info(f"\n{'='*60}")
        logger.info(f"处理: {fpath.name}")
        logger.info(f"大小: {fpath.stat().st_size / 1024:.1f} KB")
        logger.info(f"{'='*60}")

        try:
            count = processor.process_and_distill(fpath)
            logger.info(f"✅ 生成 {count} 个 Wiki 页面")
        except Exception as e:
            logger.exception(f"❌ 处理失败: {e}")


if __name__ == "__main__":
    main()
