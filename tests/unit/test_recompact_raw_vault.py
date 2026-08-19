"""Characterization tests for scripts/recompact_raw_vault.py.

These tests lock the CLI behavior and side effects of recompact_raw_vault.main()
before cyclomatic-complexity refactoring.  All heavy collaborators
(ObsidianBackend, RawIndex, core.config.get_config) are mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts import recompact_raw_vault as rv


@pytest.fixture
def raw_vault(tmp_path: Path) -> Path:
    """A minimal raw vault with .obsidian and a few fragmented .md files."""
    vault = tmp_path / "raw"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    day_dir = vault / "2024" / "01" / "15"
    day_dir.mkdir(parents=True)

    def append_file(path: Path, session_id: str, turn: int, body: str, date: str, time: str) -> None:
        fm = (
            f"---\n"
            f"date: {date}\n"
            f"time: '{time}'\n"
            f"session_id: {session_id}\n"
            f"turn: {turn}\n"
            f"source: claude\n"
            f"---\n"
            f"{body}\n"
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(fm)

    # Fragmented: multiple sessions appended into the same file
    chunk = day_dir / "chunk.md"
    append_file(chunk, "sess-b", 1, "body b1", "2024-01-15", "1000")
    append_file(chunk, "sess-a", 0, "body a0", "2024-01-14", "0900")
    append_file(chunk, "sess-b", 0, "body b0", "2024-01-15", "0900")

    # Separate small file
    other_dir = vault / "2024" / "01" / "16"
    other_dir.mkdir(parents=True)
    append_file(other_dir / "solo.md", "sess-c", 0, "body c0", "2024-01-16", "1200")

    # Session index file
    (vault / ".mnemos_session_index.json").write_text('{"idx": 1}', encoding="utf-8")
    return vault


@pytest.fixture
def mock_backend():
    """Mock ObsidianBackend and its save() results."""
    backend_cls = MagicMock()
    backend_inst = MagicMock()
    backend_inst.daily_size_threshold = 819200
    backend_inst.save.return_value = [MagicMock(uid="uid-1")]
    backend_cls.return_value = backend_inst
    return backend_cls, backend_inst


@pytest.fixture
def mock_index():
    """Mock RawIndex and its sync_index result."""
    idx_cls = MagicMock()
    idx_inst = MagicMock()
    idx_inst.sync_index.return_value = {"indexed": 3, "removed": 0, "skipped": 0}
    idx_cls.return_value = idx_inst
    return idx_cls, idx_inst


def _run_main(argv: list[str]) -> int:
    with patch.object(sys, "argv", ["recompact_raw_vault.py"] + argv):
        return rv.main()


# ── CLI argument tests ──


def test_default_raw_dir_uses_config(capsys, tmp_path: Path) -> None:
    """--raw-dir defaults to the configured raw vault path."""
    default_raw = tmp_path / "configured-raw"
    default_raw.mkdir()

    # Dry run so no side effects beyond parsing
    with patch.object(rv, "_default_raw_dir", return_value=default_raw):
        with patch.object(rv, "_compute_size_summary", return_value=(0, 0, [])):
            with patch.object(rv, "_collect_blocks", return_value=[]) as collect:
                rc = _run_main([])
    assert rc == 0
    collect.assert_called_once()
    assert str(collect.call_args[0][0]) == str(default_raw)


def test_raw_dir_cli_override(raw_vault: Path, capsys) -> None:
    """--raw-dir can be overridden on the command line."""
    with patch.object(rv, "_collect_blocks") as collect:
        with patch.object(rv, "_compute_size_summary", return_value=(0, 0, [])):
            collect.return_value = []
            rc = _run_main(["--raw-dir", str(raw_vault)])
    assert rc == 0
    collect.assert_called_once_with(raw_vault)


def test_dry_run_stats_and_exit_zero(raw_vault: Path, capsys) -> None:
    """--dry-run prints stats and exits 0 without side effects."""
    rc = _run_main(["--raw-dir", str(raw_vault), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 个 .md 文件" in out
    assert "4 个 turn block" in out
    assert "3 个 session" in out
    assert "MB" in out or "KB" in out
    # No files should be removed in dry-run mode
    assert list(raw_vault.rglob("*.md"))


# ── helper / side-effect tests ──


def test_collect_blocks(raw_vault: Path) -> None:
    """_collect_blocks returns every (file, frontmatter, body) tuple."""
    blocks = rv._collect_blocks(raw_vault)
    assert len(blocks) == 4
    session_turns = {(b[1]["session_id"], b[1]["turn"]) for b in blocks}
    assert session_turns == {("sess-a", 0), ("sess-b", 0), ("sess-b", 1), ("sess-c", 0)}


def test_compute_size_summary(raw_vault: Path) -> None:
    """_compute_size_summary reports total and max sizes in bytes."""
    md_files = sorted(raw_vault.rglob("*.md"))
    total, max_size, files = rv._compute_size_summary(md_files)
    assert files == md_files
    assert total == sum(f.stat().st_size for f in md_files)
    assert max_size == max(f.stat().st_size for f in md_files)


def test_backup_includes_md_and_index(raw_vault: Path, tmp_path: Path) -> None:
    """_backup_raw_vault copies *.md and .mnemos_session_index.json."""
    backup_dir = tmp_path / "backup"
    md_files = sorted(raw_vault.rglob("*.md"))
    rv._backup_raw_vault(raw_vault, backup_dir, md_files)

    assert (backup_dir / ".mnemos_session_index.json").exists()
    backed_md = sorted(backup_dir.rglob("*.md"))
    assert len(backed_md) == len(md_files)
    # Originals untouched
    assert list(raw_vault.rglob("*.md"))


def test_clear_raw_vault_deletes_data_and_empty_dirs(raw_vault: Path) -> None:
    """_clear_raw_vault removes data and empty YYYY/MM/DD dirs but keeps .obsidian."""
    md_files = sorted(raw_vault.rglob("*.md"))
    rv._clear_raw_vault(raw_vault, md_files)

    assert not list(raw_vault.rglob("*.md"))
    assert not (raw_vault / "2024" / "01" / "15").exists()
    assert not (raw_vault / "2024" / "01" / "16").exists()
    assert not (raw_vault / "2024" / "01").exists()
    assert not (raw_vault / "2024").exists()
    assert (raw_vault / ".obsidian").exists()


def test_rewrite_blocks_sort_order(raw_vault: Path, mock_backend) -> None:
    """_rewrite_blocks sorts by (datetime, session_id, turn) before saving."""
    backend_cls, backend_inst = mock_backend
    blocks = rv._collect_blocks(raw_vault)
    uids = rv._rewrite_blocks(raw_vault, blocks, backend_cls)

    # Expected order: sess-a 2024-01-14, sess-b turn0 2024-01-15, sess-b turn1 2024-01-15, sess-c 2024-01-16
    calls = backend_inst.save.call_args_list
    assert len(calls) == 4
    titles = [c.args[2] for c in calls]
    assert titles == [
        "claude-sess-a-turn1",
        "claude-sess-b-turn1",
        "claude-sess-b-turn2",
        "claude-sess-c-turn1",
    ]
    assert len(uids) == 4
    assert uids[0][1] == "sess-a"


def test_update_sync_log_uids(raw_vault: Path, tmp_path: Path) -> None:
    """_update_sync_log_uids updates matching rows and ignores missing db."""
    db = tmp_path / "sync_log.db"
    conn = __import__("sqlite3").connect(str(db))
    conn.execute(
        "CREATE TABLE sync_log (agent_name TEXT, session_id TEXT, turn_number INTEGER, backend_uids TEXT)"
    )
    conn.executemany(
        "INSERT INTO sync_log VALUES (?, ?, ?, ?)",
        [("claude", "sess-a", 0, ""), ("claude", "sess-b", 0, "")],
    )
    conn.commit()
    conn.close()

    rows = [("claude", "sess-a", 0, "uid-a0"), ("claude", "sess-b", 0, "uid-b0")]
    assert rv._update_sync_log_uids(db, rows) == 2

    # Missing db returns 0
    assert rv._update_sync_log_uids(tmp_path / "nope.db", rows) == 0


def test_rebuild_raw_index(raw_vault: Path, mock_index) -> None:
    """_rebuild_raw_index instantiates RawIndex, syncs, and closes it."""
    idx_cls, idx_inst = mock_index
    stats = rv._rebuild_raw_index(raw_vault, idx_cls)
    idx_cls.assert_called_once_with(raw_dir=raw_vault)
    idx_inst.sync_index.assert_called_once_with(force_full=True)
    idx_inst.close.assert_called_once()
    assert stats == {"indexed": 3, "removed": 0, "skipped": 0}


def test_print_recompact_report(tmp_path: Path, capsys) -> None:
    """_print_recompact_report prints Chinese MB/KB lines and backup location."""
    raw_vault = tmp_path / "raw"
    raw_vault.mkdir()
    (raw_vault / "f.md").write_text("x" * 1024, encoding="utf-8")
    new_files = sorted(raw_vault.rglob("*.md"))
    backup = raw_vault / "backup"
    rv._print_recompact_report(
        raw_dir=raw_vault,
        turn_count=4,
        new_files=new_files,
        threshold=819200,
        backup_dir=backup,
    )
    out = capsys.readouterr().out
    assert "recompact 完成" in out
    assert "写入 turn 数: 4" in out
    assert "生成文件数: 1" in out
    assert "总大小:" in out and "MB" in out
    assert "最大文件:" in out and "KB" in out
    assert "超阈值" in out
    assert str(backup) in out


# ── full main smoke with mocked collaborators ──


def test_main_full_run(
    raw_vault: Path,
    tmp_path: Path,
    mock_backend,
    mock_index,
    monkeypatch,
) -> None:
    """main() performs backup, rewrite, sync-log update, and index rebuild."""
    backend_cls, backend_inst = mock_backend
    idx_cls, idx_inst = mock_index

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    db = tmp_path / "sync_log.db"
    conn = __import__("sqlite3").connect(str(db))
    conn.execute(
        "CREATE TABLE sync_log (agent_name TEXT, session_id TEXT, turn_number INTEGER, backend_uids TEXT)"
    )
    conn.commit()
    conn.close()

    fake_cfg = MagicMock()
    fake_cfg.database_dir = db.parent

    with patch("integrations.backends.obsidian_backend.ObsidianBackend", backend_cls):
        with patch("core.app.raw_search.RawIndex", idx_cls):
            with patch("core.config.get_config", return_value=fake_cfg):
                rc = _run_main(["--raw-dir", str(raw_vault)])

    assert rc == 0
    # Backup created under ~/.mnemos/backups
    backups = list((fake_home / ".mnemos" / "backups").glob("raw-recompact-*"))
    assert len(backups) == 1
    assert (backups[0] / ".mnemos_session_index.json").exists()
    # Rewritten
    assert backend_inst.save.call_count == 4
    # Index rebuilt
    assert idx_inst.sync_index.called
