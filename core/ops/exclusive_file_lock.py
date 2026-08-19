"""Cross-platform advisory locks for offline migration writer exclusion."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Iterator


class ExclusiveFileLockError(RuntimeError):
    """A requested migration or daemon exclusion lock is already held."""


@contextmanager
def exclusive_file_lock(
    path: Path,
    *,
    unavailable_message: str,
) -> Iterator[None]:
    """Hold one non-blocking byte/file lock on POSIX or Windows."""

    with _file_lock(
        path,
        unavailable_message=unavailable_message,
        shared=False,
    ):
        yield


@contextmanager
def shared_file_lock(
    path: Path,
    *,
    unavailable_message: str,
) -> Iterator[None]:
    """Hold a shared runtime-writer lock against offline migrations.

    POSIX permits multiple runtime readers.  Windows uses the conservative
    one-byte exclusive primitive because ``msvcrt`` has no shared equivalent.
    """

    with _file_lock(
        path,
        unavailable_message=unavailable_message,
        shared=True,
    ):
        yield


@contextmanager
def _file_lock(
    path: Path,
    *,
    unavailable_message: str,
    shared: bool,
) -> Iterator[None]:
    """Hold one non-blocking advisory lock for the requested lifetime."""

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt  # type: ignore[import]

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(  # type: ignore[attr-defined]
                    descriptor,
                    msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                    1,
                )
            else:
                import fcntl

                mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            locked = True
        except (BlockingIOError, IOError, OSError) as exc:
            raise ExclusiveFileLockError(unavailable_message) from exc
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt  # type: ignore[import]

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(  # type: ignore[attr-defined]
                        descriptor,
                        msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                        1,
                    )
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        else:
            os.close(descriptor)
