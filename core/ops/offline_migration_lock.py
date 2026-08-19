"""Shared offline-migration and daemon-lifetime exclusion guard."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
import threading

from core.migrations.model_call_ledger_reconcile.runtime import (
    runtime_writers_are_inactive,
)
from core.ops.exclusive_file_lock import exclusive_file_lock, shared_file_lock

_LOCK_STATE = threading.local()


def _held_roots() -> dict[Path, int]:
    held = getattr(_LOCK_STATE, "held_roots", None)
    if held is None:
        held = {}
        _LOCK_STATE.held_roots = held
    return held


@contextmanager
def offline_migration_lock(
    database_dir: Path,
    *,
    daemon_check: Callable[[Path], bool] = runtime_writers_are_inactive,
) -> Iterator[None]:
    """Hold the process-wide migration lock and daemon lifetime lock.

    The read-only process check happens immediately before lock acquisition.
    If a runtime starts in that interval, it owns ``daemon.pid`` and the second
    lock fails.  Once both locks are held, neither another migration nor the
    daemon can begin until the complete file/SQLite/manifest transaction exits.
    """

    root = Path(database_dir).expanduser().resolve(strict=False)
    held = _held_roots()
    depth = held.get(root, 0)
    if depth:
        held[root] = depth + 1
        try:
            yield
        finally:
            if held[root] == 1:
                held.pop(root, None)
            else:
                held[root] -= 1
        return
    if not daemon_check(root):
        raise RuntimeError("all Mnemos daemon and MCP writers must be stopped")
    with exclusive_file_lock(
        root / ".mnemos_offline_migration.lock",
        unavailable_message="another Mnemos offline migration is active",
    ):
        with exclusive_file_lock(
            root / "daemon.pid",
            unavailable_message="Mnemos daemon started before migration lock",
        ):
            with exclusive_file_lock(
                root / ".mnemos_runtime_writer.lock",
                unavailable_message="Mnemos MCP writer started before migration lock",
            ):
                held[root] = 1
                try:
                    yield
                finally:
                    held.pop(root, None)


@contextmanager
def runtime_writer_lock(database_dir: Path) -> Iterator[None]:
    """Hold the shared runtime side of the migration/writer exclusion lock."""

    root = Path(database_dir).expanduser().resolve(strict=False)
    with shared_file_lock(
        root / ".mnemos_runtime_writer.lock",
        unavailable_message="Mnemos offline migration is active; runtime writer cannot start",
    ):
        yield


__all__ = ["offline_migration_lock", "runtime_writer_lock"]
