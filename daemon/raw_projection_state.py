"""Pure state and path helpers for the canonical Raw vault projection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from core.ops.durable_io import (
    DurableIOError,
    inspect_path_kind,
    secure_atomic_write_text,
    secure_read_bytes,
)


def raw_projection_state_path(cfg: Any) -> Path:
    """Return the state file used to avoid redundant raw vault rewrites."""
    return Path(cfg.database_dir) / "raw_projection_state.json"


def raw_projection_signature(
    *,
    db_path: Path,
    raw_dir: Path,
    stats: Dict[str, Any],
    expected_path_hash: str,
    max_files: int,
    chunk_turns: int,
    max_turn_chars: int,
    max_file_bytes: int = 0,
    include_eligible_delete: bool,
) -> Dict[str, Any]:
    """Build the complete projection input/output signature."""
    db_kind = inspect_path_kind(db_path)
    if db_kind not in {"missing", "file"}:
        raise DurableIOError("raw_projection_database_not_regular")
    db_stat = db_path.stat() if db_kind == "file" else None
    return {
        "version": 2,
        "db_path": str(db_path),
        "raw_dir": str(raw_dir),
        "db_mtime_ns": db_stat.st_mtime_ns if db_stat else 0,
        "db_size": db_stat.st_size if db_stat else 0,
        "candidate_turns": stats.get("candidate_turns", 0),
        "projected_files": stats.get("projected_files", 0),
        "projected_sources": stats.get("projected_sources", {}),
        "expected_path_hash": expected_path_hash,
        "max_files": max_files,
        "chunk_turns": chunk_turns,
        "max_turn_chars": max_turn_chars,
        "max_file_bytes": max_file_bytes,
        "include_eligible_delete": include_eligible_delete,
    }


def load_raw_projection_state(path: Path) -> Dict[str, Any]:
    """Load a projection state file, treating malformed state as a cache miss."""
    try:
        content = secure_read_bytes(path.parent, path.name)
        if content is None:
            return {}
        state = json.loads(content.decode("utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def write_raw_projection_state(path: Path, state: Dict[str, Any]) -> None:
    """Persist projection state using a deterministic JSON representation."""
    # trusted-scan: system_state owner=raw_projection target=raw_projection_state expires=never
    secure_atomic_write_text(
        path.parent,
        path.name,
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
    )


def raw_projection_actual_paths(raw_dir: Path) -> List[str]:
    """Return current raw vault markdown paths, ignoring Obsidian metadata."""
    raw_dir_kind = inspect_path_kind(raw_dir)
    if raw_dir_kind == "missing":
        return []
    if raw_dir_kind != "directory":
        raise DurableIOError("raw_projection_root_not_directory")
    paths: List[str] = []
    for path in raw_dir.rglob("*.md"):
        rel = path.relative_to(raw_dir)
        if ".obsidian" in rel.parts:
            continue
        paths.append(rel.as_posix())
    return sorted(paths)


def raw_projection_expected_paths(
    raw_dir: Path,
    chunks: List[Any],
    projection: Any,
) -> List[str]:
    """Return the publisher-owned path denominator for a projection plan."""
    # A paged chunk's part count is only known after rendering, so this state
    # signature keeps listing each chunk's base path: the base path is stable
    # whether the chunk publishes as one file or as an index page plus parts.
    return sorted(
        projection._chunk_path(raw_dir, chunk).relative_to(raw_dir).as_posix()
        for chunk in chunks
    )


def hash_projection_paths(paths: List[str]) -> str:
    """Hash the ordered projection path denominator."""
    return hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest()
