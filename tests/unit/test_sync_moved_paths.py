import sqlite3
from pathlib import Path

from core.frontmatter import parse_frontmatter
from scripts.sync_moved_paths import (
    _build_name_index,
    _update_kg_source_pages,
    _update_shadow_files,
)


def test_build_name_index_prefers_non_inbox_duplicate(tmp_path: Path):
    wiki = tmp_path / "wiki"
    inbox = wiki / "00-Inbox"
    knowledge = wiki / "01-Knowledge"
    inbox.mkdir(parents=True)
    knowledge.mkdir(parents=True)
    inbox_page = inbox / "Same.md"
    knowledge_page = knowledge / "Same.md"
    inbox_page.write_text("old", encoding="utf-8")
    knowledge_page.write_text("new", encoding="utf-8")

    index = _build_name_index(wiki)

    assert index["Same.md"] == knowledge_page


def test_update_kg_source_pages_repoints_missing_paths(tmp_path: Path):
    db_path = tmp_path / "kg.db"
    new_page = tmp_path / "wiki" / "01-Knowledge" / "Moved.md"
    new_page.parent.mkdir(parents=True)
    new_page.write_text("# Moved", encoding="utf-8")
    old_page = tmp_path / "old" / "Moved.md"

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE entities (uid TEXT PRIMARY KEY, source_page TEXT)")
        conn.execute("INSERT INTO entities (uid, source_page) VALUES (?, ?)", ("u1", str(old_page)))

    updated = _update_kg_source_pages(db_path, {"Moved.md": new_page})

    assert updated == 1
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT source_page FROM entities WHERE uid = ?", ("u1",)).fetchone()
    assert row == (str(new_page),)


def test_update_shadow_files_repoints_missing_shadow_for(tmp_path: Path):
    wiki = tmp_path / "wiki"
    shadow_dir = wiki / "07-Shadow"
    new_page = wiki / "01-Knowledge" / "Moved.md"
    shadow_dir.mkdir(parents=True)
    new_page.parent.mkdir(parents=True)
    new_page.write_text("# Moved", encoding="utf-8")
    old_page = tmp_path / "old" / "Moved.md"
    shadow_file = shadow_dir / "example.shadow.md"
    shadow_file.write_text(
        f"---\nshadow_for: {old_page}\n---\n\nbody\n",
        encoding="utf-8",
    )

    updated = _update_shadow_files(wiki, {"Moved.md": new_page})

    assert updated == 1
    frontmatter, body = parse_frontmatter(shadow_file.read_text(encoding="utf-8"))
    assert frontmatter["shadow_for"] == str(new_page)
    assert body.strip() == "body"
