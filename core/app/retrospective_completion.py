"""Terminal-page verification for retrospective workflow receipts."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def reopen_missing_terminal_page(
    manager: Any, session: Any, wiki_dir: Path
) -> dict[str, Any] | None:
    """Reopen a claimed terminal recap when its committed page cannot be verified."""
    page_path = str(session.finalized_page or "")
    if page_path and (Path(wiki_dir) / page_path).exists():
        return None
    failed_receipt = {
        **session.completion_receipt,
        "status": "retryable_failed",
        "terminal": False,
        "terminal_reason": "finalized_retrospective_page_is_missing",
    }
    manager.mark_pipeline_state(
        session.recap_id,
        "retryable_failed",
        page_path=page_path,
        completion_receipt=failed_receipt,
    )
    return {
        "success": False,
        "status": "retryable_failed",
        "state": "retryable_failed",
        "terminal": False,
        "page_path": page_path,
        "indexed": False,
        "error": failed_receipt["terminal_reason"],
    }
