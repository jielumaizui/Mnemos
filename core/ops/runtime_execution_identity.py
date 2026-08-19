"""Canonical execution-runtime identity for exact offline migration plans."""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path
import sqlite3
import sys
from typing import Any

import psutil

from core.ops.durable_io import DurableIOError, regular_file_sha256


class RuntimeExecutionIdentityError(RuntimeError):
    """The current interpreter/runtime cannot be bound safely."""


def _sha256_file(path: Path) -> str:
    try:
        digest = regular_file_sha256(path)
    except (DurableIOError, OSError):
        raise RuntimeExecutionIdentityError(
            "runtime_executable_identity_unavailable"
        ) from None
    return f"sha256:{digest}"


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def runtime_execution_identity() -> dict[str, Any]:
    """Return the runtime inputs that can change parse/SQLite migration behavior."""

    executable = Path(sys.executable).expanduser().resolve(strict=True)
    try:
        with sqlite3.connect(":memory:") as connection:
            compile_options = sorted(
                str(row[0])
                for row in connection.execute("PRAGMA compile_options").fetchall()
            )
    except sqlite3.Error:
        raise RuntimeExecutionIdentityError(
            "sqlite_runtime_identity_unavailable"
        ) from None
    executable_identity = {
        "resolved_path": str(executable),
        "sha256": _sha256_file(executable),
    }
    return {
        "schema_version": "mnemos.runtime_execution_identity.v1",
        "python_executable_identity_hash": _sha256_json(executable_identity),
        "python_executable_sha256": executable_identity["sha256"],
        "python_version": sys.version,
        "python_implementation": platform.python_implementation(),
        "python_cache_tag": str(sys.implementation.cache_tag or ""),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
        "sqlite_runtime_version": sqlite3.sqlite_version,
        "sqlite_threadsafety": int(sqlite3.threadsafety),
        "sqlite_compile_options": compile_options,
        "psutil_version": str(psutil.__version__),
    }


__all__ = [
    "RuntimeExecutionIdentityError",
    "runtime_execution_identity",
]
