"""Tests for ObsidianBackend file sharding and collision handling."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from integrations.backends.obsidian_backend import ObsidianBackend


@pytest.fixture
def backend(tmp_path):
    """返回一个禁用自动注册 Obsidian vault 的 backend 实例。"""
    with patch.object(ObsidianBackend, "_ensure_vault_recognized", lambda self: None):
        yield ObsidianBackend(vault_path=tmp_path, daily_size_threshold=500)


def _tags(session: str, turn: int = 1, source: str = "kimi") -> list[str]:
    return [
        f"source={source}",
        f"session={session}",
        f"turn={turn}",
        "time=20260101",
    ]


def test_new_session_gets_unique_part_when_file_exists(backend, tmp_path):
    """不同 session 不应复用同一个 part 文件名，避免被追加进同一文件。"""
    now = datetime(2026, 1, 1, 14, 0)
    backend.save("session A turn 1\n", _tags("session-a", 1), "title-a", now=now)
    backend.save("session B turn 1\n", _tags("session-b", 1), "title-b", now=now)

    files = sorted((tmp_path / "2026" / "01" / "01").glob("*.md"))
    names = [f.name for f in files]
    assert len(names) == 2
    assert names == sorted(names)

    # 两个文件内容应独立
    contents = [f.read_text(encoding="utf-8") for f in files]
    assert any("session A" in c for c in contents)
    assert any("session B" in c for c in contents)
    assert not all("session A" in c and "session B" in c for c in contents)


def test_same_session_appends_until_threshold_then_splits(backend, tmp_path):
    """同 session 在阈值内追加，超过阈值后新建 part。"""
    session = "session-c"
    now = datetime(2026, 1, 1, 14, 0)
    # 每次约 200B，500B 阈值应能容纳 2 次，第三次触发新文件
    for turn in range(1, 5):
        backend.save(
            f"turn {turn} content\n" + "x" * 180 + "\n",
            _tags(session, turn),
            f"title-{turn}",
            now=now,
        )

    files = sorted((tmp_path / "2026" / "01" / "01").glob("*.md"))
    assert len(files) >= 2

    # 所有 turn 都被写入
    all_content = "\n".join(f.read_text(encoding="utf-8") for f in files)
    for turn in range(1, 5):
        assert f"turn {turn} content" in all_content

    # 至少有一个文件没超过阈值；最大文件不超过阈值 + 一次写入大小（约 400B）
    sizes = [f.stat().st_size for f in files]
    assert max(sizes) < 500 + 250


def test_save_uses_provided_now_timestamp(backend, tmp_path):
    """save() 的 now 参数应决定文件存放的日期目录。"""
    past = datetime(2024, 3, 15, 10, 30)
    backend.save("past content\n", _tags("session-past", 1), "title-past", now=past)

    expected_file = tmp_path / "2024" / "03" / "15"
    assert expected_file.exists()
    files = list(expected_file.glob("*.md"))
    assert len(files) == 1
    assert "past content" in files[0].read_text(encoding="utf-8")


def test_save_without_now_uses_today(backend, tmp_path):
    """未提供 now 时，文件应放在当天目录下。"""
    backend.save("now content\n", _tags("session-now", 1), "title-now")

    today = datetime.now()
    expected_dir = tmp_path / str(today.year) / f"{today.month:02d}" / f"{today.day:02d}"
    assert expected_dir.exists()
    files = list(expected_dir.glob("*.md"))
    assert len(files) == 1


def test_raw_projection_blocks_legacy_raw_vault_write(tmp_path):
    """raw_projection 接管 raw vault 时，legacy ObsidianBackend 不应再直写 raw md。"""

    class FakeConfig:
        obsidian_vault_path = tmp_path

        @staticmethod
        def get(key, default=None):
            return {"raw_projection.enabled": True}.get(key, default)

    with (
        patch("integrations.backends.obsidian_backend.get_config", lambda: FakeConfig()),
        patch.object(ObsidianBackend, "_ensure_vault_recognized", lambda self: None),
    ):
        backend = ObsidianBackend(vault_path=tmp_path)
        result = backend.save("blocked\n", _tags("blocked-session", 1), "title")

    assert result == []
    assert list(tmp_path.rglob("*.md")) == []


def test_legacy_raw_vault_write_allowed_when_projection_disabled(tmp_path):
    """关闭 raw_projection 时，legacy ObsidianBackend 仍可作为兼容兜底写入。"""

    class FakeConfig:
        obsidian_vault_path = tmp_path

        @staticmethod
        def get(key, default=None):
            return {"raw_projection.enabled": False}.get(key, default)

    with (
        patch("integrations.backends.obsidian_backend.get_config", lambda: FakeConfig()),
        patch.object(ObsidianBackend, "_ensure_vault_recognized", lambda self: None),
    ):
        backend = ObsidianBackend(vault_path=tmp_path)
        result = backend.save("allowed\n", _tags("legacy-session", 1), "title")

    assert len(result) == 1
    assert len(list(tmp_path.rglob("*.md"))) == 1
