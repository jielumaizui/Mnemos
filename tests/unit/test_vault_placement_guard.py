from __future__ import annotations

from pathlib import Path


def test_move_page_to_category_blocks_global_formal_basename_duplicate(
    tmp_path: Path, monkeypatch
):
    from core.kia import charon

    wiki = tmp_path
    inbox = wiki / "00-Inbox"
    tech = wiki / "03-Tech"
    python_dir = tech / "python"
    inbox.mkdir()
    python_dir.mkdir(parents=True)

    existing = tech / "python-pptx.md"
    existing.write_text("# python-pptx\n\nExisting formal page\n", encoding="utf-8")
    incoming = inbox / "python-pptx.md"
    incoming.write_text("# python-pptx\n\nDifferent incoming page\n", encoding="utf-8")

    class FakeConfig:
        wiki_dir = wiki

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())

    result = charon._move_page_to_category(incoming, python_dir, dry_run=False)

    assert result["status"] == "duplicate_basename"
    assert Path(result["existing"][0]) == existing
    assert incoming.exists()
    assert not (python_dir / "python-pptx.md").exists()


def test_move_page_to_category_blocks_title_collision_for_source_prefixed_file(
    tmp_path: Path, monkeypatch
):
    from core.kia import charon

    wiki = tmp_path
    inbox = wiki / "00-Inbox"
    tech = wiki / "03-Tech"
    python_dir = tech / "python"
    inbox.mkdir()
    python_dir.mkdir(parents=True)

    existing = tech / "python-pptx.md"
    existing.write_text("# python-pptx\n\nExisting formal page\n", encoding="utf-8")
    incoming = inbox / "codex-20_python-pptx.md"
    incoming.write_text(
        "---\n类型: technology\n名称: python-pptx\n领域: python\n摘要: 已结构化页面\n---\n# python-pptx\n",
        encoding="utf-8",
    )

    class FakeConfig:
        wiki_dir = wiki

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())

    result = charon._move_page_to_category(incoming, python_dir, dry_run=False)

    assert result["status"] == "duplicate_basename"
    assert Path(result["existing"][0]) == existing
    assert result["to"].endswith("python-pptx.md")
    assert incoming.exists()


def test_move_page_to_category_renames_inbox_source_prefixed_file_to_title(
    tmp_path: Path,
    monkeypatch,
    _canonical_material_actions,
):
    from core.kia import charon

    wiki = tmp_path
    inbox = wiki / "00-Inbox"
    target = wiki / "03-Tech" / "redis"
    inbox.mkdir()
    target.mkdir(parents=True)
    incoming = inbox / "codex-20_redis-ttl策略.md"
    incoming.write_text(
        "---\n类型: technology\n名称: Redis TTL策略\n领域: redis\n摘要: 已结构化页面\n---\n# Redis TTL策略\n",
        encoding="utf-8",
    )

    class FakeConfig:
        wiki_dir = wiki

    monkeypatch.setattr("core.config.get_config", lambda: FakeConfig())

    result = charon._move_page_to_category(incoming, target, dry_run=False)

    assert result["status"] == "moved"
    assert (target / "redis-ttl策略.md").exists()
    assert not incoming.exists()
