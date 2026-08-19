"""Tests for the raw-index CLI maintenance command."""

from __future__ import annotations

import json
import sqlite3
import sys
from argparse import Namespace


def test_raw_index_status_reports_missing_db(tmp_path, monkeypatch, capsys, fake_config):
    from core.cli.commands.raw_index import cmd_raw_index

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    db_path = tmp_path / "raw_index.db"
    monkeypatch.setattr("core.cli.commands.raw_index.get_config", lambda: fake_config)

    args = Namespace(
        raw_index_cmd="status",
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        json=False,
    )

    assert cmd_raw_index(args) == 0
    output = capsys.readouterr().out
    assert "status:" in output
    assert "missing" in output
    assert "stale:" in output
    assert "yes" in output
    assert not db_path.exists()


def test_raw_index_rebuild_dry_run_does_not_create_db(
    tmp_path, monkeypatch, capsys, fake_config
):
    from core.cli.commands.raw_index import cmd_raw_index

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "note.md").write_text("# Note\nhello", encoding="utf-8")
    db_path = tmp_path / "raw_index.db"
    monkeypatch.setattr("core.cli.commands.raw_index.get_config", lambda: fake_config)

    args = Namespace(
        raw_index_cmd="rebuild",
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        incremental=False,
        apply=False,
        json=True,
    )

    assert cmd_raw_index(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is False
    assert report["markdown_files"] == 1
    assert report["stats"] is None
    assert not db_path.exists()


def test_raw_index_rebuild_apply_indexes_files(tmp_path, monkeypatch, capsys, fake_config):
    from core.cli.commands.raw_index import cmd_raw_index

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "note.md").write_text("# Note\nhello", encoding="utf-8")
    db_path = tmp_path / "raw_index.db"
    monkeypatch.setattr("core.cli.commands.raw_index.get_config", lambda: fake_config)

    args = Namespace(
        raw_index_cmd="rebuild",
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        incremental=False,
        apply=True,
        json=True,
    )

    assert cmd_raw_index(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is True
    assert report["stats"]["indexed"] == 1
    assert report["after"]["indexed_files"] == 1

    with sqlite3.connect(str(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM raw_index").fetchone()[0]
    assert count == 1


def test_raw_index_rebuild_apply_reports_errors(tmp_path, monkeypatch, capsys, fake_config):
    from core.cli.commands.raw_index import cmd_raw_index

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    db_path = tmp_path / "raw_index.db"
    monkeypatch.setattr("core.cli.commands.raw_index.get_config", lambda: fake_config)

    class FailingRawIndex:
        def __init__(self, **kwargs):
            pass

        def sync_index(self, force_full: bool = False):
            raise RuntimeError("boom")

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "core.app.raw_search",
        type("M", (), {"RawIndex": FailingRawIndex}),
    )
    args = Namespace(
        raw_index_cmd="rebuild",
        raw_dir=str(raw_dir),
        db_path=str(db_path),
        incremental=False,
        apply=True,
        json=True,
    )

    assert cmd_raw_index(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "boom"
    assert report["applied"] is True
