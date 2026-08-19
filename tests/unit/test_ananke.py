"""
Tests for core.kia.ananke (VersionTimeTravel)

Covers: snapshot, list_versions, diff, restore, generate_timeline,
        scan_and_snapshot_all, _detect_frontmatter_changes, _detect_section_changes.
"""

from unittest.mock import patch


from core.kia.ananke import (
    VersionTimeTravel,
    VersionSnapshot,
    snapshot_page,
    show_diff,
)


class TestVersionTimeTravelInit:
    def test_init_default(self, tmp_path):
        with patch("core.kia.ananke.get_config") as mock_cfg:
            mock_cfg.return_value.wiki_dir = tmp_path
            vt = VersionTimeTravel()
            assert vt.wiki_base == tmp_path
            assert vt.snapshot_dir.exists()

    def test_init_explicit_wiki(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        assert vt.wiki_base == tmp_path
        assert vt.snapshot_dir.exists()


class TestSnapshot:
    def test_snapshot_new_page(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        page.write_text("# Hello\n\nContent", encoding="utf-8")

        result = vt.snapshot(page)
        assert result is not None
        assert isinstance(result, VersionSnapshot)
        assert result.snapshot_id
        assert result.timestamp
        assert result.size_bytes > 0

        # 快照文件应存在
        snapshot_file = vt.snapshot_dir / f"{result.snapshot_id}.md"
        assert snapshot_file.exists()

    def test_snapshot_unchanged_returns_none(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        page.write_text("# Hello", encoding="utf-8")

        r1 = vt.snapshot(page)
        assert r1 is not None
        r2 = vt.snapshot(page)
        assert r2 is None  # 内容未变化

    def test_snapshot_nonexistent_returns_none(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        result = vt.snapshot(tmp_path / "nonexistent.md")
        assert result is None


class TestListVersions:
    def test_list_versions(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        page.write_text("v1", encoding="utf-8")
        vt.snapshot(page, change_summary="first")
        page.write_text("v2", encoding="utf-8")
        vt.snapshot(page, change_summary="second")

        versions = vt.list_versions(page)
        assert len(versions) == 2
        assert versions[0].change_summary == "first"
        assert versions[1].change_summary == "second"

    def test_list_versions_empty(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        versions = vt.list_versions(page)
        assert versions == []


class TestGetVersionContent:
    def test_get_version_content(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        page.write_text("original", encoding="utf-8")
        snap = vt.snapshot(page)

        content = vt.get_version_content(snap.snapshot_id)
        assert content == "original"

    def test_get_version_content_missing(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        assert vt.get_version_content("nonexistent") is None


class TestDiff:
    def test_diff_between_versions(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        page.write_text("line1\nline2\n", encoding="utf-8")
        vt.snapshot(page)
        page.write_text("line1\nline2 modified\nline3\n", encoding="utf-8")
        vt.snapshot(page)

        diff = vt.diff(page)
        assert diff is not None
        assert diff.added_lines
        assert diff.removed_lines

    def test_diff_insufficient_versions(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        page.write_text("only one", encoding="utf-8")
        vt.snapshot(page)

        diff = vt.diff(page)
        assert diff is None

    def test_diff_specific_snapshots(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        page.write_text("v1", encoding="utf-8")
        s1 = vt.snapshot(page)
        page.write_text("v2", encoding="utf-8")
        s2 = vt.snapshot(page)

        diff = vt.diff(page, from_snapshot=s1.snapshot_id, to_snapshot=s2.snapshot_id)
        assert diff is not None
        assert diff.from_version == s1.snapshot_id
        assert diff.to_version == s2.snapshot_id

    def test_show_diff_uses_explicit_wiki_base(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        page.write_text("v1", encoding="utf-8")
        vt.snapshot(page)
        page.write_text("v2", encoding="utf-8")
        vt.snapshot(page)

        rendered = show_diff(str(page), wiki_base=str(tmp_path))

        assert rendered is not None
        assert "版本对比" in rendered
        assert "v2" in rendered


class TestRestore:
    def test_restore(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        page.write_text("original", encoding="utf-8")
        snap = vt.snapshot(page)
        page.write_text("modified", encoding="utf-8")

        ok = vt.restore(page, snap.snapshot_id)
        assert ok is True
        assert page.read_text(encoding="utf-8") == "original"

    def test_restore_nonexistent_snapshot(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        page.write_text("x", encoding="utf-8")
        ok = vt.restore(page, "nonexistent")
        assert ok is False

    def test_restore_creates_backup(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        page.write_text("original", encoding="utf-8")
        snap = vt.snapshot(page)
        page.write_text("modified", encoding="utf-8")

        vt.restore(page, snap.snapshot_id, create_backup=True)
        versions = vt.list_versions(page)
        # 备份 + 恢复 = 至少 3 个版本（原始、修改、备份、恢复）
        assert len(versions) >= 3


class TestGenerateTimeline:
    def test_timeline_format(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        page.write_text("v1", encoding="utf-8")
        vt.snapshot(page, change_summary="first edit")

        timeline = vt.generate_timeline(page)
        assert "# 版本时间线" in timeline
        assert "first edit" in timeline

    def test_empty_timeline(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        page = tmp_path / "test.md"
        timeline = vt.generate_timeline(page)
        assert "暂无版本历史" in timeline


class TestDiffToMarkdown:
    def test_diff_markdown(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        from core.kia.ananke import VersionDiff

        diff = VersionDiff(
            from_version="abc123",
            to_version="def456",
            added_lines=["new line"],
            removed_lines=["old line"],
            frontmatter_changes={"title": {"old": "A", "new": "B"}},
            modified_sections=[{"name": "Intro", "action": "修改"}],
        )
        md = vt.diff_to_markdown(diff)
        assert "# 版本对比" in md
        assert "Frontmatter 变更" in md
        assert "new line" in md
        assert "old line" in md


class TestScanAndSnapshotAll:
    def test_scan_inbox(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        inbox = tmp_path / "00-Inbox"
        inbox.mkdir()
        (inbox / "page1.md").write_text("content1", encoding="utf-8")
        (inbox / "page2.md").write_text("content2", encoding="utf-8")

        stats = vt.scan_and_snapshot_all()
        assert stats["scanned"] == 2
        assert stats["snapshotted"] == 2

    def test_scan_no_inbox(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        stats = vt.scan_and_snapshot_all()
        assert stats["scanned"] == 0


class TestDetectFrontmatterChanges:
    def test_detect_changes(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        old = "---\ntitle: A\ntags: [x]\n---\nbody"
        new = "---\ntitle: B\ntags: [x, y]\n---\nbody"
        changes = vt._detect_frontmatter_changes(old, new)
        assert "title" in changes
        assert changes["title"]["old"] == "A"
        assert changes["title"]["new"] == "B"

    def test_no_frontmatter(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        changes = vt._detect_frontmatter_changes("plain", "text")
        assert changes == {}


class TestDetectSectionChanges:
    def test_detect_added_section(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        old = "body\n"
        new = "body\n## New Section\ncontent\n"
        changes = vt._detect_section_changes(old, new)
        assert any(c["name"] == "New Section" and c["action"] == "新增" for c in changes)

    def test_detect_removed_section(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        old = "body\n## Old Section\ncontent\n"
        new = "body\n"
        changes = vt._detect_section_changes(old, new)
        assert any(c["name"] == "Old Section" and c["action"] == "删除" for c in changes)

    def test_detect_modified_section(self, tmp_path):
        vt = VersionTimeTravel(wiki_base=str(tmp_path))
        old = "body\n## Same\nold content\n"
        new = "body\n## Same\nnew content\n"
        changes = vt._detect_section_changes(old, new)
        assert any(c["name"] == "Same" and c["action"] == "修改" for c in changes)


class TestConvenienceFunctions:
    def test_snapshot_page(self, tmp_path):
        page = tmp_path / "test.md"
        page.write_text("hello", encoding="utf-8")
        with patch("core.kia.ananke.get_config") as mock_cfg:
            mock_cfg.return_value.wiki_dir = tmp_path
            result = snapshot_page(str(page))
            assert result is not None
