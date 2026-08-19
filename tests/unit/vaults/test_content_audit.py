from __future__ import annotations

import argparse
from pathlib import Path


def test_audit_vault_content_reports_display_classification_and_structure(
    tmp_path: Path,
):
    from core.vaults.content_audit import audit_vault_content

    inbox = tmp_path / "00-Inbox"
    tech = tmp_path / "03-Tech"
    people = tmp_path / "01-People"
    retros = tmp_path / "06-Retrospectives"
    inbox.mkdir()
    tech.mkdir()
    people.mkdir()
    retros.mkdir()

    (inbox / "codex-20_redis.md").write_text(
        "---\n类型: technology\n名称: Redis\n领域: redis\n摘要: 已结构化但仍在 Inbox\n---\n# Redis\n",
        encoding="utf-8",
    )
    (tech / "redis.md").write_text(
        "---\n类型: technology\n名称: Redis\n领域: redis\n摘要: 正式页\n---\n# Redis\n",
        encoding="utf-8",
    )
    (people / "redis-person.md").write_text(
        "---\n类型: person\n名称: Redis\n领域: redis\n摘要: 正式页重名\n---\n# Redis\n",
        encoding="utf-8",
    )
    (inbox / "needs-review.md").write_text(
        (
            "---\n"
            "类型: concept\n"
            "名称: Needs Review\n"
            "领域: review\n"
            "摘要: 待人工确认\n"
            "needs_review: true\n"
            "---\n"
            "# Needs Review\n"
        ),
        encoding="utf-8",
    )
    (tech / "needs-review.md").write_text(
        "---\n类型: concept\n名称: Needs Review\n领域: review\n摘要: 正式页\n---\n# Needs Review\n",
        encoding="utf-8",
    )
    (retros / "missing.md").write_text("# Missing frontmatter\n", encoding="utf-8")

    report = audit_vault_content(tmp_path)

    assert report["display"]["source_prefixed_filenames"] == 1
    assert report["classification"]["inbox_ready_to_classify"] == 1
    assert report["classification"]["needs_review_pages"] == 1
    assert report["classification"]["title_basename_collision_groups"] == 1
    assert report["structured_output"]["frontmatter_problem_pages"] == 1
    assert report["structured_output"]["formal_missing_required_fields"] == 1


def test_vaults_audit_content_cli_prints_summary(
    tmp_path: Path, monkeypatch, capsys
):
    from core.cli.commands import vaults

    (tmp_path / "00-Inbox").mkdir()
    (tmp_path / "00-Inbox" / "codex-20_redis.md").write_text(
        "---\n类型: technology\n名称: Redis\n领域: redis\n摘要: 已结构化\n---\n# Redis\n",
        encoding="utf-8",
    )

    class FakeConfig:
        def vault_dir(self, name: str) -> Path:
            assert name == "mnemos"
            return tmp_path

    monkeypatch.setattr(vaults, "_get_config", lambda: FakeConfig())

    args = argparse.Namespace(vaults_cmd="audit-content", json=False)
    assert vaults.cmd_vaults(args) == 0

    captured = capsys.readouterr()
    assert "Vault content audit" in captured.out
    assert "source_prefixed_filenames: 1" in captured.out
