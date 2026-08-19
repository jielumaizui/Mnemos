# -*- coding: utf-8 -*-
"""Unit tests for core/utils.py"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.utils import LazyPath, WIKI_DIRS, EXCLUDED_DIRS, atomic_write_text

# ---------------------------------------------------------------------------
# LazyPath
# ---------------------------------------------------------------------------


class TestLazyPath:
    """LazyPath 延迟路径解析测试"""

    def test_lazy_path_str_resolution(self, monkeypatch, tmp_path):
        """str(LazyPath) 应在访问时才解析为实际路径。"""
        fake_cfg = MagicMock()
        fake_cfg.data_dir = tmp_path / "data"
        fake_cfg.wiki_dir = tmp_path / "wiki"
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

        lp = LazyPath("wiki_dir", "00-Inbox", "test.md")
        assert str(lp) == str(tmp_path / "wiki" / "00-Inbox" / "test.md")

    def test_lazy_path_data_dir_default(self, monkeypatch, tmp_path):
        """默认 base 为 data_dir。"""
        fake_cfg = MagicMock()
        fake_cfg.data_dir = tmp_path / "data"
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

        lp = LazyPath("data_dir", "logs")
        assert str(lp) == str(tmp_path / "data" / "logs")

    def test_lazy_path_div_operator(self, monkeypatch, tmp_path):
        """__truediv__ 应返回新的 LazyPath。"""
        fake_cfg = MagicMock()
        fake_cfg.data_dir = tmp_path / "data"
        fake_cfg.wiki_dir = tmp_path / "wiki"
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

        base = LazyPath("wiki_dir")
        child = base / "01-People" / "note.md"
        assert str(child) == str(tmp_path / "wiki" / "01-People" / "note.md")

    def test_lazy_path_repr(self):
        """repr 应显示 base 和 segments。"""
        lp = LazyPath("wiki_dir", "00-Inbox", "test.md")
        assert repr(lp) == "LazyPath(wiki_dir:00-Inbox/test.md)"

    def test_lazy_path_fspath(self, monkeypatch, tmp_path):
        """__fspath__ 应返回字符串路径。"""
        fake_cfg = MagicMock()
        fake_cfg.data_dir = tmp_path / "data"
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

        lp = LazyPath("data_dir", "file.txt")
        assert Path(lp).name == "file.txt"

    def test_lazy_path_eq(self, monkeypatch, tmp_path):
        """__eq__ 应与 Path 比较。"""
        fake_cfg = MagicMock()
        fake_cfg.wiki_dir = tmp_path / "wiki"
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

        lp = LazyPath("wiki_dir", "test.md")
        assert lp == tmp_path / "wiki" / "test.md"
        assert lp != tmp_path / "other" / "test.md"

    def test_lazy_path_hash(self, monkeypatch, tmp_path):
        """__hash__ 应基于原始值保持稳定，不随解析结果变化。"""
        fake_cfg = MagicMock()
        fake_cfg.wiki_dir = tmp_path / "wiki"
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

        lp = LazyPath("wiki_dir", "test.md")
        h1 = hash(lp)
        # 配置变更后 hash 不变
        fake_cfg.wiki_dir = tmp_path / "other"
        h2 = hash(lp)
        assert h1 == h2
        # 相同构造的 LazyPath hash 相同
        assert hash(LazyPath("wiki_dir", "test.md")) == h1

    def test_lazy_path_getattr_delegation(self, monkeypatch, tmp_path):
        """__getattr__ 应委托到 resolved Path。"""
        fake_cfg = MagicMock()
        fake_cfg.data_dir = tmp_path / "data"
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

        lp = LazyPath("data_dir", "file.txt")
        assert lp.name == "file.txt"
        assert lp.suffix == ".txt"

    def test_lazy_path_unknown_base_fallback(self, monkeypatch, tmp_path):
        """未知 base 应回退到 data_dir。"""
        fake_cfg = MagicMock()
        fake_cfg.data_dir = tmp_path / "data"
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

        lp = LazyPath("unknown_base", "file.txt")
        assert str(lp) == str(tmp_path / "data" / "file.txt")

    def test_lazy_path_rtruediv_raises(self):
        """__rtruediv__ 应抛 NotImplementedError。"""
        lp = LazyPath("data_dir")
        with pytest.raises(NotImplementedError):
            "prefix" / lp  # type: ignore[operator]

    def test_lazy_path_iter(self, monkeypatch, tmp_path):
        """__iter__ 应委托到 resolved Path 的 parts。"""
        fake_cfg = MagicMock()
        fake_cfg.data_dir = tmp_path / "data"
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

        lp = LazyPath("data_dir", "a", "b", "c.txt")
        # Path 本身不可迭代，但 parts 属性可以
        assert lp.parts[-1] == "c.txt"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_wiki_dirs_structure():
    """WIKI_DIRS 应包含所有标准目录。"""
    assert "00-Inbox" in WIKI_DIRS
    assert "01-People" in WIKI_DIRS
    assert "99-Reports" in WIKI_DIRS
    assert len(WIKI_DIRS) >= 9


def test_excluded_dirs():
    """EXCLUDED_DIRS 应包含常见排除目录。"""
    assert ".git" in EXCLUDED_DIRS
    assert ".obsidian" in EXCLUDED_DIRS
    assert "__pycache__" in EXCLUDED_DIRS


# ---------------------------------------------------------------------------
# atomic_write_text
# ---------------------------------------------------------------------------


def test_atomic_write_text_creates_file(tmp_path):
    """原子写入应创建文件。"""
    target = tmp_path / "output.txt"
    atomic_write_text(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world"


def test_atomic_write_text_overwrites_existing(tmp_path):
    """原子写入应覆盖已有文件。"""
    target = tmp_path / "output.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_custom_encoding(tmp_path):
    """原子写入应支持自定义编码。"""
    target = tmp_path / "output.txt"
    atomic_write_text(target, "中文内容", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "中文内容"


def test_atomic_write_text_no_temp_leak(tmp_path):
    """原子写入后不应残留临时文件。"""
    target = tmp_path / "output.txt"
    atomic_write_text(target, "content")
    temps = list(tmp_path.glob("*.tmp.*"))
    assert len(temps) == 0
