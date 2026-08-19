"""Asynchronous post-distillation embedding-index refresh."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any


def trigger_embedding_index_refresh(wiki_base: str | None) -> None:
    """Refresh the derived index without delaying a committed distillation."""

    def worker() -> None:
        try:
            from core.embeddings import EmbeddingIndexManager

            resolved_wiki_base = Path(wiki_base).expanduser() if wiki_base else None
            stats: dict[str, Any] = EmbeddingIndexManager(
                wiki_base=resolved_wiki_base
            ).build_index(force_full=False)
            if stats.get("added", 0) or stats.get("updated", 0):
                logging.getLogger(__name__).info(
                    "[Embedding] 增量索引更新完成: +%s ~%s -%s",
                    stats.get("added", 0),
                    stats.get("updated", 0),
                    stats.get("removed", 0),
                )
        except (OSError, ValueError, TypeError, ImportError, AttributeError, RuntimeError):
            logging.getLogger(__name__).debug(
                "[Embedding] 增量索引更新失败（可能是未配置 embedding）",
                exc_info=True,
            )

    threading.Thread(target=worker, daemon=True, name="EmbedIndexUpdate").start()
