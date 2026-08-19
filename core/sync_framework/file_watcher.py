"""Stat-only file change watcher for raw vault and wiki paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat
from typing import Dict, Iterable, List, Tuple


@dataclass(frozen=True)
class FileChange:
    path: str
    change_type: str  # added | modified | deleted
    old_mtime: float | None = None
    new_mtime: float | None = None
    old_size: int | None = None
    new_size: int | None = None


Snapshot = Tuple[float, int]


class FileWatcherUnavailableError(RuntimeError):
    """The watched scope could not be inspected without inventing deletions."""


class StatFileWatcher:
    """Detect file changes without reading file contents."""

    def __init__(
        self,
        watch_dir: Path,
        patterns: Iterable[str] = ("*.md",),
        enabled: bool = True,
    ):
        self.watch_dir = Path(watch_dir).expanduser()
        self.patterns = tuple(patterns)
        self.enabled = enabled
        self._snapshot: Dict[str, Snapshot] = {}

    def scan(self) -> List[FileChange]:
        """Return coalesced changes since the previous scan."""
        if not self.enabled:
            return []

        current = self._collect_snapshot()
        changes: List[FileChange] = []

        for path, state in current.items():
            old = self._snapshot.get(path)
            if old is None:
                changes.append(
                    FileChange(
                        path=path,
                        change_type="added",
                        new_mtime=state[0],
                        new_size=state[1],
                    )
                )
            elif old != state:
                changes.append(
                    FileChange(
                        path=path,
                        change_type="modified",
                        old_mtime=old[0],
                        new_mtime=state[0],
                        old_size=old[1],
                        new_size=state[1],
                    )
                )

        for path, old in self._snapshot.items():
            if path not in current:
                changes.append(
                    FileChange(
                        path=path,
                        change_type="deleted",
                        old_mtime=old[0],
                        old_size=old[1],
                    )
                )

        self._snapshot = current
        changes.sort(key=lambda change: (change.path, change.change_type))
        return changes

    def prime(self) -> None:
        """Load the initial snapshot without emitting changes."""
        self._snapshot = self._collect_snapshot()

    def _collect_snapshot(self) -> Dict[str, Snapshot]:
        try:
            root_metadata = self.watch_dir.stat()
        except FileNotFoundError:
            return {}
        except OSError:
            raise FileWatcherUnavailableError(
                "file_watcher_root_unavailable"
            ) from None
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise FileWatcherUnavailableError("file_watcher_root_not_directory")

        snapshot: Dict[str, Snapshot] = {}
        try:
            for pattern in self.patterns:
                for file_path in self.watch_dir.rglob(pattern):
                    try:
                        metadata = file_path.stat()
                    except FileNotFoundError:
                        continue
                    except OSError:
                        raise FileWatcherUnavailableError(
                            "file_watcher_entry_unavailable"
                        ) from None
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    snapshot[str(file_path)] = (
                        metadata.st_mtime,
                        metadata.st_size,
                    )
        except FileWatcherUnavailableError:
            raise
        except OSError:
            raise FileWatcherUnavailableError(
                "file_watcher_scan_unavailable"
            ) from None
        return snapshot
