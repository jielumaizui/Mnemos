# -*- coding: utf-8 -*-
"""External link probe service helper for the daemon."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, cast


def run_service(
    cfg: Any,
    *,
    log_service_error: Callable[[str, Exception], None],
    log_info: Callable[..., None] | None = None,
) -> Dict[str, Any]:
    """Run a bounded external link probe batch."""
    result = {"enabled": True, "probed": 0, "broken": 0, "updated": 0, "errors": 0}
    try:
        from core.config import get_config
        from core.hephaestus.link_probe_worker import LinkProbeWorker

        if cfg is None:
            cfg = get_config()
        if not cfg.get("features.enable_link_probe", False):
            result["enabled"] = False
            return result
        if not cfg.get("daemon.services.link_probe", False):
            result["enabled"] = False
            return result

        worker = LinkProbeWorker()
        batch = cast(List[Dict[str, Any]], worker.probe_batch(batch_size=50))
        result["probed"] = len(batch)
        broken = [item for item in batch if item.get("status") == "broken"]
        result["broken"] = len(broken)

        updated_pages = set()
        for item in batch:
            page_path = item.get("page_path")
            if page_path and worker.update_wiki_frontmatter(page_path):
                updated_pages.add(page_path)
        result["updated"] = len(updated_pages)

        if result["probed"] and log_info is not None:
            log_info(
                "[DAEMON] Link probe: %d probed, %d broken, %d pages updated",
                result["probed"],
                result["broken"],
                result["updated"],
            )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        log_service_error("link_probe", exc)
        result["errors"] += 1
    return result
