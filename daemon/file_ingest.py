# -*- coding: utf-8 -*-
"""File ingestion helpers for the daemon."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional


def resolve_ingest_dir(cfg: Any, data_dir: Path) -> Optional[Path]:
    """Resolve FileIngestor watch directory from config or default data dir."""
    if cfg is not None:
        path_str = cfg.get("file_ingestor.watch_dir", "")
        if path_str:
            return Path(path_str).expanduser()
    return data_dir / "file_ingest"


def run_service(
    cfg: Any,
    *,
    data_dir: Path,
    log_service_error: Callable[[str, Exception], None],
) -> Dict[str, int]:
    """Scan the ingest directory and enqueue canonical raw document captures."""
    result = {"ingested": 0, "errors": 0}
    try:
        from core.sync_framework.file_ingestor import FileIngestor

        if cfg is None:
            from core.config import get_config

            cfg = get_config()
        ingest_dir = resolve_ingest_dir(cfg, data_dir)
        if ingest_dir is None or not ingest_dir.exists():
            return result

        ingestor = FileIngestor(config=cfg)
        result["ingested"] = ingestor.ingest_directory(
            ingest_dir,
            agent_name="file",
            recursive=True,
        )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("file_ingestor", exc)
        result["errors"] += 1
    return result
