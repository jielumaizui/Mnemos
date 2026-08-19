from pathlib import Path

from core.frontmatter import parse_frontmatter
from core.trust.markdown_adapter import MarkdownAdapter


def test_markdown_conflict_inherits_frontmatter_and_adds_metadata(tmp_path: Path):
    page = tmp_path / "page.md"
    page.write_text("---\ntitle: Existing\nowner: user\n---\n# Existing\n", encoding="utf-8")

    result = MarkdownAdapter(tmp_path).write(
        page,
        "# Proposed\n",
        conflict_metadata={"proposal_id": "prop1"},
    )

    assert result.status == "conflict"
    assert result.conflict_path is not None
    fm, body = parse_frontmatter(result.conflict_path.read_text(encoding="utf-8"))
    assert fm["title"] == "Existing"
    assert fm["owner"] == "user"
    assert fm["mnemos_conflict"]["proposal_id"] == "prop1"
    assert "# Proposed" in body
