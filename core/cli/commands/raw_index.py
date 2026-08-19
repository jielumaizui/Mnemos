"""RawIndex maintenance commands."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def get_config():
    from core.config import get_config as load_config

    return load_config()


def _raw_index_paths(args: Any | None = None) -> tuple[Path, Path]:
    cfg = get_config()
    raw_dir_arg = getattr(args, "raw_dir", None) if args is not None else None
    db_path_arg = getattr(args, "db_path", None) if args is not None else None
    raw_dir = Path(raw_dir_arg).expanduser() if raw_dir_arg else Path(cfg.obsidian_vault_path)
    db_path = Path(db_path_arg).expanduser() if db_path_arg else Path(cfg.database_dir) / "raw_index.db"
    return raw_dir, db_path


def _count_markdown_files(raw_dir: Path) -> int:
    if not raw_dir.exists():
        return 0
    return sum(1 for path in raw_dir.rglob("*.md") if path.is_file())


def _table_count(conn: sqlite3.Connection, table_name: str) -> int:
    count_queries = {
        "raw_index": "SELECT COUNT(*) FROM raw_index",
        "raw_fts": "SELECT COUNT(*) FROM raw_fts",
    }
    if table_name not in count_queries:
        raise ValueError(f"unsupported raw index table: {table_name}")
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name=?",
        (table_name,),
    ).fetchone()
    if not exists:
        return 0
    return int(conn.execute(count_queries[table_name]).fetchone()[0])


def _read_raw_index_health(args: Any | None = None) -> dict:
    raw_dir, db_path = _raw_index_paths(args)
    markdown_files = _count_markdown_files(raw_dir)
    if not db_path.exists():
        return {
            "status": "missing",
            "indexed_files": 0,
            "fts_entries": 0,
            "db_size_mb": 0,
            "raw_dir": str(raw_dir),
            "db_path": str(db_path),
            "raw_dir_exists": raw_dir.exists(),
            "db_exists": False,
            "markdown_files": markdown_files,
            "stale": True,
            "last_sync_at": None,
        }

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        total = _table_count(conn, "raw_index")
        fts_total = _table_count(conn, "raw_fts")
        last_sync_at = None
        if total:
            row = conn.execute("SELECT MAX(indexed_at) FROM raw_index").fetchone()
            last_sync_at = row[0] if row and row[0] is not None else None
    return {
        "status": "ok" if total > 0 or not raw_dir.exists() else "empty",
        "indexed_files": total,
        "fts_entries": fts_total,
        "db_size_mb": round(db_path.stat().st_size / (1024 * 1024), 2),
        "raw_dir": str(raw_dir),
        "db_path": str(db_path),
        "raw_dir_exists": raw_dir.exists(),
        "db_exists": True,
        "markdown_files": markdown_files,
        "stale": markdown_files != total,
        "last_sync_at": last_sync_at,
    }


def _print_status(health: dict, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(health, ensure_ascii=False, indent=2))
        return
    print("RawIndex 状态")
    print("=" * 40)
    print(f"status:        {health.get('status', 'unknown')}")
    print(f"indexed_files: {health.get('indexed_files', 0)}")
    print(f"fts_entries:   {health.get('fts_entries', 0)}")
    print(f"db_size_mb:    {health.get('db_size_mb', 0)}")
    print(f"raw_dir:       {health.get('raw_dir', '')}")
    print(f"db_path:       {health.get('db_path', '')}")
    print(f"markdown_files:{health.get('markdown_files', 0)}")
    print(f"stale:         {'yes' if health.get('stale') else 'no'}")
    print(f"last_sync_at:  {health.get('last_sync_at') or 'unknown'}")


def cmd_raw_index(args) -> int:
    """Maintain raw_index.db without hiding write operations behind status calls."""
    if args.raw_index_cmd == "status":
        _print_status(_read_raw_index_health(args), as_json=getattr(args, "json", False))
        return 0

    if args.raw_index_cmd == "rebuild":
        health = _read_raw_index_health(args)
        force_full = not getattr(args, "incremental", False)
        raw_dir = Path(health.get("raw_dir", ""))
        db_path = Path(health.get("db_path", ""))
        result = {
            "applied": bool(getattr(args, "apply", False)),
            "force_full": force_full,
            "raw_dir": str(raw_dir),
            "db_path": str(db_path),
            "markdown_files": health.get("markdown_files", 0),
            "before": health,
            "stats": None,
            "after": None,
        }
        if not raw_dir.exists():
            result["error"] = "raw_dir_missing"
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"raw vault 不存在: {raw_dir}")
            return 1

        if not getattr(args, "apply", False):
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print("RawIndex rebuild dry-run")
                print("=" * 40)
                print(f"raw_dir: {result['raw_dir']}")
                print(f"db_path: {result['db_path']}")
                print(f"markdown_files: {result['markdown_files']}")
                print(f"force_full: {'yes' if force_full else 'no'}")
                print("未执行写入；加 --apply 才会重建 raw_index.db")
            return 0

        from core.app.raw_search import RawIndex

        index = None
        try:
            index = RawIndex(raw_dir=raw_dir, db_path=db_path, config=get_config())
            result["stats"] = index.sync_index(force_full=force_full)
            result["after"] = index.health_check()
        except (
            AttributeError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ) as exc:
            result["error"] = str(exc)
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"RawIndex 重建失败: {exc}")
            return 1
        finally:
            if index is not None:
                index.close()
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("RawIndex rebuild complete")
            print("=" * 40)
            stats = result["stats"] or {}
            for key in ["indexed", "removed", "skipped"]:
                print(f"{key}: {stats.get(key, 0)}")
            after = result["after"] or {}
            print(f"indexed_files: {after.get('indexed_files', 0)}")
            print(f"fts_entries:   {after.get('fts_entries', 0)}")
        return 0

    print("用法: mnemos raw-index {status|rebuild}")
    return 1
