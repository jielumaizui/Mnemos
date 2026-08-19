"""Bounded daemon-thread execution with exact exception propagation."""

from __future__ import annotations

import threading
from typing import Any, Callable


class _ThreadCallOutcome:
    """Carry a daemon-thread failure back to the calling thread."""

    def __init__(self) -> None:
        self.value: Any = None
        self.error: BaseException | None = None

    def __enter__(self) -> "_ThreadCallOutcome":
        return self

    def __exit__(self, _exc_type: Any, exc: BaseException | None, _tb: Any) -> bool:
        self.error = exc
        return exc is not None


def run_daemon_call(call: Callable[[], Any], *, timeout: float) -> tuple[bool, Any]:
    """Run one hook subtask and re-raise its exact failure in the caller."""

    outcome = _ThreadCallOutcome()

    def _target() -> None:
        with outcome:
            outcome.value = call()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        return True, None
    if outcome.error is not None:
        raise outcome.error
    return False, outcome.value
