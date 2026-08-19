# -*- coding: utf-8 -*-
"""Scoring signal detection services for the daemon."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict


def run_search_ignore_detection(
    log_service_error: Callable[[str, Exception], None],
    *,
    log_info: Callable[..., None] | None = None,
    now_func: Callable[[], datetime] = datetime.now,
) -> Dict[str, int]:
    """Retired: elapsed silence is neither user reaction nor ground truth."""

    del log_service_error, log_info, now_func
    return {"ignored": 0}


def run_user_correction_detection(
    log_service_error: Callable[[str, Exception], None],
    *,
    log_info: Callable[..., None] | None = None,
    log_warning: Callable[..., None] | None = None,
) -> Dict[str, int]:
    """Retired: file mtime drift lacks exact user attribution and semantic proof."""

    del log_service_error, log_info, log_warning
    return {"corrections": 0}
