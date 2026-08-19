from __future__ import annotations

from pathlib import Path

import pytest

from core.ops.exclusive_file_lock import (
    ExclusiveFileLockError,
    exclusive_file_lock,
)


def test_exclusive_file_lock_rejects_a_second_writer_and_releases(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "migration.lock"

    with exclusive_file_lock(lock_path, unavailable_message="already held"):
        with pytest.raises(ExclusiveFileLockError, match="already held"):
            with exclusive_file_lock(
                lock_path,
                unavailable_message="already held",
            ):
                pytest.fail("a second writer acquired the same lock")

    with exclusive_file_lock(lock_path, unavailable_message="already held"):
        assert lock_path.is_file()
