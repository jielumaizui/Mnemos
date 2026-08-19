# -*- coding: utf-8 -*-
"""Tests for freshness CLI."""

from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.cli.commands.freshness import cmd_freshness


@pytest.fixture
def wiki(tmp_path):
    base = tmp_path / "wiki"
    base.mkdir()
    return base


def _write_page(wiki: Path, rel: str, fm: dict, body: str = ""):
    p = wiki / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = "\n".join(f"{k}: {v}" for k, v in fm.items())
    content = f"---\n{fm_lines}\n---\n\n{body}"
    p.write_text(content, encoding="utf-8")


@pytest.fixture  # noqa
def monkeypatch_config(monkeypatch, wiki):
    class Cfg:
        wiki_dir = wiki
        database_dir = wiki / ".mnemos"

    monkeypatch.setattr("core.cli.commands.freshness._config_mod.get_config", lambda: Cfg())


def test_freshness_list(capsys, wiki, monkeypatch_config):  # noqa
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    _write_page(wiki, "stale.md", {"updated_at": old}, "body")
    args = Namespace(freshness_cmd="list", status="stale")
    ret = cmd_freshness(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "stale" in out
    assert "stale.md" in out


def test_freshness_refresh(
    capsys,
    wiki,
    monkeypatch_config,
):  # noqa
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    _write_page(wiki, "target.md", {"updated_at": old}, "body")
    args = Namespace(freshness_cmd="refresh", page_path="target.md")
    ret = cmd_freshness(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "已刷新" in out


def test_freshness_refresh_all(
    capsys,
    wiki,
    monkeypatch_config,
):  # noqa
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    _write_page(wiki, "a.md", {"updated_at": old}, "body")
    args = Namespace(freshness_cmd="refresh-all", limit=5)
    ret = cmd_freshness(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "refreshed=1" in out
