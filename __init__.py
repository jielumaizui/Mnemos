# -*- coding: utf-8 -*-
"""
Mnemos Embedding 模块 — 语义搜索与向量索引

依赖（可选安装）:
    pip install hnswlib openai

配置路径:
    ~/.mnemos/configs/main.json → embedding 字段
"""

from __future__ import annotations

from .siliconflow_client import SiliconFlowEmbeddingClient, get_embedding_client
from .index_manager import EmbeddingIndexManager

__all__ = [
    "SiliconFlowEmbeddingClient",
    "get_embedding_client",
    "EmbeddingIndexManager",
]
