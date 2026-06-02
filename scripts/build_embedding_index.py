#!/usr/bin/env python3
"""
Mnemos Embedding 索引构建脚本

用法:
    python3 scripts/build_embedding_index.py [--force]

选项:
    --force  强制全量重建（默认增量更新）

环境要求:
    - embedding.enabled = true in ~/.mnemos/configs/main.json
    - pip install hnswlib openai  (可选，hnswlib 缺失时回退到内存模式)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main():
    parser = argparse.ArgumentParser(description="Mnemos Embedding 索引构建")
    parser.add_argument("--force", action="store_true", help="强制全量重建")
    args = parser.parse_args()

    try:
        from core.config import get_config
        cfg = get_config()
        if not cfg.get("embedding.enabled", False):
            print("❌ 语义搜索未启用。请在配置中设置 embedding.enabled = true")
            print("   配置文件: ~/.mnemos/configs/main.json")
            sys.exit(1)
    except Exception as e:
        print(f"❌ 配置读取失败: {e}")
        sys.exit(1)

    try:
        from core.embeddings import EmbeddingIndexManager
        idx = EmbeddingIndexManager()
        stats = idx.get_stats()
        print(f"索引目录: {stats['index_dir']}")
        print(f"Wiki 目录: {stats['wiki_base']}")
        print(f"hnswlib: {'可用' if stats['hnswlib_available'] else '不可用（回退到内存模式）'}")
        print(f"客户端: {'可用' if stats['client_available'] else '不可用'}")
        print()

        if not stats["client_available"]:
            print("❌ Embedding 客户端不可用。请检查:")
            print("   1. API Key 是否配置（embedding.api_key 或 SILICONFLOW_API_KEY 环境变量）")
            print("   2. 网络是否可达硅基流动 API")
            sys.exit(1)

        print("开始构建索引...")
        result = idx.build_index(force_full=args.force)
        print()
        print(f"✅ 索引构建完成")
        print(f"   新增: {result['added']} 页")
        print(f"   更新: {result['updated']} 页")
        print(f"   删除: {result['removed']} 页")
        print(f"   总计: {result['total']} 页")
        print(f"   后端: {result.get('backend', 'unknown')}")

    except Exception as e:
        print(f"❌ 索引构建失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
