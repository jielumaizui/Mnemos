# -*- coding: utf-8 -*-
"""Unit tests for core.app.obsidian_opener."""

from __future__ import annotations

from unittest.mock import patch


from core.app.obsidian_opener import _build_uri, open_obsidian


class TestBuildUri:
    def test_build_uri_encodes_vault_and_file(self):
        uri = _build_uri("My Vault", "01-People/note.md")
        assert uri == "obsidian://open?vault=My%20Vault&file=01-People/note"

    def test_build_uri_no_md_suffix(self):
        uri = _build_uri("Vault", "dashboard")
        assert uri == "obsidian://open?vault=Vault&file=dashboard"


class TestOpenObsidian:
    def test_open_with_uri_directly(self):
        with patch("core.app.obsidian_opener._open_uri", return_value=True) as mock_open:
            result = open_obsidian(uri="obsidian://open?vault=x&file=y")
        assert result is True
        mock_open.assert_called_once_with("obsidian://open?vault=x&file=y")

    def test_open_with_vault_and_page(self):
        with patch("core.app.obsidian_opener._open_uri", return_value=True) as mock_open:
            result = open_obsidian(page_path="00-Dashboard.md", vault_name="Vault")
        assert result is True
        mock_open.assert_called_once()
        args = mock_open.call_args[0][0]
        assert args.startswith("obsidian://open?vault=Vault&file=00-Dashboard")

    def test_open_file_fallback_when_uri_fails(self, tmp_path, monkeypatch):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        page = wiki_dir / "note.md"
        page.write_text("content", encoding="utf-8")

        fake_cfg = type("Config", (), {"wiki_dir": wiki_dir})()
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

        with patch("core.app.obsidian_opener._open_uri", return_value=False) as mock_uri:
            with patch("core.app.obsidian_opener._open_file", return_value=True) as mock_file:
                result = open_obsidian(page_path="note.md", vault_name="Vault")
        assert result is True
        mock_uri.assert_called_once()
        mock_file.assert_called_once_with("note.md")

    def test_open_file_page_not_exists(self, tmp_path, monkeypatch):
        fake_cfg = type("Config", (), {"wiki_dir": tmp_path})()
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)
        with patch("core.app.obsidian_opener._open_file", return_value=False) as _:
            result = open_obsidian(page_path="missing.md")
        assert result is False
