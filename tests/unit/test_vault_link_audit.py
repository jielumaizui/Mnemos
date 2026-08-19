import argparse
from pathlib import Path

from core.vaults.link_audit import (
    audit_vault_links,
    repair_broken_wikilinks,
    repair_vault_absolute_wikilinks,
    render_link_audit_report,
    render_link_repair_report,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _FakeConfig:
    def __init__(self, vault: Path) -> None:
        self._vault = vault

    def vault_dir(self, name: str) -> Path:
        assert name == "mnemos"
        return self._vault


def test_audit_vault_links_resolves_obsidian_wikilink_variants(tmp_path):
    vault = tmp_path / "wiki"
    _write(
        vault / "Home.md",
        "- [[Sub/Target|target]]\n"
        "- [[Target#Heading]]\n"
        "- [[#Local]]\n"
        "- [[assets/a.png]]\n"
        "- [[Upper.MD]]",
    )
    _write(vault / "Sub" / "Target.md", "ok")
    _write(vault / "Upper.MD", "ok")
    _write(vault / "assets" / "a.png", "fake image bytes")
    _write(vault / ".obsidian" / "Ignored.md", "[[Missing]]")

    report = audit_vault_links(vault)

    assert report.ok is True
    assert report.total_pages == 3
    assert report.total_links == 4
    assert report.broken_links == 0


def test_audit_vault_links_missing_vault_is_error_report(tmp_path):
    report = audit_vault_links(tmp_path / "missing")

    assert report.ok is False
    assert report.error.startswith("vault path does not exist")
    assert report.to_dict()["error"] == report.error
    assert "错误:" in render_link_audit_report(report)


def test_audit_vault_links_attachment_stem_does_not_hide_missing_note(tmp_path):
    vault = tmp_path / "wiki"
    _write(vault / "Home.md", "- [[Missing]]\n- [[assets/Missing.png]]")
    _write(vault / "assets" / "Missing.png", "fake image bytes")

    report = audit_vault_links(vault)

    assert report.ok is False
    assert report.total_links == 2
    assert report.broken_links == 1
    assert report.samples[0].target == "Missing"


def test_audit_vault_links_uses_frontmatter_aliases(tmp_path):
    vault = tmp_path / "wiki"
    _write(vault / "Home.md", "- [[English Alias]]\n- [[中文别名]]")
    _write(
        vault / "Target.md",
        "---\naliases:\n  - English Alias\n别名:\n  - 中文别名\n---\nbody",
    )

    report = audit_vault_links(vault)

    assert report.ok is True
    assert report.total_links == 2
    assert report.broken_links == 0


def test_audit_vault_links_resolves_generated_page_name_aliases(tmp_path):
    vault = tmp_path / "wiki"
    _write(
        vault / "Home.md",
        "- [[03-Tech/hermes-双-profile-工作流中个人-profile-应只保留必要配置和技能]]\n"
        "- [[03-Tech/claude-code-的本地对话记录在哪里查找]]\n"
        "- [[03-Tech/codex-trace日志高频写盘导致ssd过度损耗的排查与修复方法]]\n",
    )
    _write(vault / "03-Tech/session__hermes-双-profile-工作流中个人-profile-应只保留必要配置和技能.md", "ok")
    _write(vault / "03-Tech/c49fb3d5_claude-code-的本地对话记录在哪里查找.md", "ok")
    _write(vault / "03-Tech/10-47-56_codex-trace日志高频写盘导致ssd过度损耗的排查与修复方法.md", "ok")

    report = audit_vault_links(vault)

    assert report.ok is True
    assert report.total_links == 3
    assert report.broken_links == 0


def test_audit_vault_links_uses_frontmatter_title_fields_as_aliases(tmp_path):
    vault = tmp_path / "wiki"
    _write(vault / "Home.md", "- [[Primary Name]]\n- [[中文标题]]")
    _write(
        vault / "Target.md",
        "---\nname: Primary Name\n标题: 中文标题\n---\nbody",
    )

    report = audit_vault_links(vault)

    assert report.ok is True
    assert report.total_links == 2
    assert report.broken_links == 0


def test_audit_vault_links_reports_broken_samples_and_top_dirs(tmp_path):
    vault = tmp_path / "wiki"
    _write(vault / "L3-Observations" / "Note.md", "- [[Missing]]\n- [[Also Missing|alias]]")
    _write(vault / "Existing.md", "ok")

    report = audit_vault_links(vault, sample_limit=1)

    assert report.ok is False
    assert report.total_links == 2
    assert report.broken_links == 2
    assert report.pages_with_broken_links == 1
    assert report.broken_by_top_dir == {"L3-Observations": 2}
    assert len(report.samples) == 1
    assert report.samples[0].page == "L3-Observations/Note.md"
    assert report.samples[0].line == 1
    assert report.samples[0].target == "Missing"

    rendered = render_link_audit_report(report)
    assert "Vault 断链审计" in rendered
    assert "审计范围: all" in rendered
    assert "L3-Observations/Note.md:1 -> [[Missing]]" in rendered


def test_audit_vault_links_filters_pages_by_scope_but_uses_all_aliases(tmp_path):
    vault = tmp_path / "wiki"
    _write(vault / "03-Tech" / "Known.md", "known")
    _write(vault / "L2.4-KG" / "Entity.md", "- [[03-Tech/Known]]\n- [[KG Missing]]")
    _write(vault / "07-Shadow" / "Shadow.md", "- [[Shadow Missing]]")

    report = audit_vault_links(vault, scope="kg")

    assert report.scope == "kg"
    assert report.total_pages == 1
    assert report.total_links == 2
    assert report.broken_links == 1
    assert report.pages_with_broken_links == 1
    assert report.broken_by_top_dir == {"L2.4-KG": 1}
    assert report.samples[0].target == "KG Missing"


def test_audit_vault_links_rejects_unknown_scope(tmp_path):
    vault = tmp_path / "wiki"
    _write(vault / "Home.md", "ok")

    try:
        audit_vault_links(vault, scope="unknown")
    except ValueError as exc:
        assert "unsupported link audit scope" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_repair_vault_absolute_wikilinks_dry_run_does_not_write(tmp_path):
    vault = tmp_path / "wiki"
    target = vault / "Sub" / "Target.md"
    _write(target, "ok")
    absolute = target.as_posix()
    source = vault / "L2.4-KG" / "Entity.md"
    _write(source, f"- [[{absolute}#Heading|alias]]\n")

    report = repair_vault_absolute_wikilinks(vault, scope="kg", sample_limit=1)

    assert report.ok is True
    assert report.applied is False
    assert report.scanned_pages == 1
    assert report.candidate_links == 1
    assert report.changed_pages == 1
    assert report.samples[0].before == f"{absolute}#Heading|alias"
    assert report.samples[0].after == "Sub/Target#Heading|alias"
    assert source.read_text(encoding="utf-8") == f"- [[{absolute}#Heading|alias]]\n"
    assert "dry-run 未写入" in render_link_repair_report(report)


def test_repair_vault_absolute_wikilinks_apply_rewrites_scoped_pages_only(tmp_path):
    vault = tmp_path / "wiki"
    target = vault / "Known.md"
    _write(target, "known")
    absolute = target.as_posix()
    kg_page = vault / "L2.4-KG" / "Entity.md"
    shadow_page = vault / "07-Shadow" / "Shadow.md"
    _write(kg_page, f"- [[{absolute}]]\n")
    _write(shadow_page, f"- [[{absolute}]]\n")

    report = repair_vault_absolute_wikilinks(vault, scope="kg", apply=True)

    assert report.applied is True
    assert report.candidate_links == 1
    assert kg_page.read_text(encoding="utf-8") == "- [[Known]]\n"
    assert shadow_page.read_text(encoding="utf-8") == f"- [[{absolute}]]\n"


def test_repair_vault_absolute_wikilinks_ignores_external_absolute_paths(tmp_path):
    vault = tmp_path / "wiki"
    source = vault / "L2.4-KG" / "Entity.md"
    _write(source, "- [[/tmp/outside.md]]\n")

    report = repair_vault_absolute_wikilinks(vault, scope="kg", apply=True)

    assert report.candidate_links == 0
    assert source.read_text(encoding="utf-8") == "- [[/tmp/outside.md]]\n"


def test_repair_broken_wikilinks_strips_only_unresolved_links(tmp_path):
    vault = tmp_path / "wiki"
    _write(vault / "Known.md", "known")
    source = vault / "03-Tech" / "Note.md"
    _write(
        source,
        "- [[Known]]\n- [[Missing Concept]]\n- [[Other Missing|Alias Text]]\n",
    )

    dry_run = repair_broken_wikilinks(vault, sample_limit=2)

    assert dry_run.mode == "strip-broken"
    assert dry_run.applied is False
    assert dry_run.candidate_links == 2
    assert dry_run.changed_pages == 1
    assert dry_run.samples[0].before == "Missing Concept"
    assert dry_run.samples[0].after == "Missing Concept"
    assert dry_run.samples[1].before == "Other Missing|Alias Text"
    assert dry_run.samples[1].after == "Alias Text"
    assert "[[Missing Concept]]" in source.read_text(encoding="utf-8")

    applied = repair_broken_wikilinks(vault, apply=True)

    assert applied.applied is True
    assert applied.candidate_links == 2
    assert source.read_text(encoding="utf-8") == (
        "- [[Known]]\n- Missing Concept\n- Alias Text\n"
    )


def test_vaults_audit_links_cli_reports_broken_links(tmp_path, monkeypatch, capsys):
    from core.cli.commands import vaults

    wiki = tmp_path / "wiki"
    _write(wiki / "Home.md", "- [[Missing]]\n")
    monkeypatch.setattr(vaults, "_get_config", lambda: _FakeConfig(wiki))

    args = argparse.Namespace(
        vaults_cmd="audit-links",
        vault=None,
        limit=5,
        scope="all",
        json=False,
    )

    assert vaults.cmd_vaults(args) == 1
    output = capsys.readouterr().out
    assert "Vault 断链审计" in output
    assert "断链: 1" in output


def test_vaults_audit_links_with_explicit_vault_does_not_read_config(
    tmp_path, monkeypatch, capsys
):
    from core.cli.commands import vaults

    wiki = tmp_path / "wiki"
    _write(wiki / "Home.md", "ok\n")
    monkeypatch.setattr(
        vaults,
        "_get_config",
        lambda: (_ for _ in ()).throw(AssertionError("config should not load")),
    )

    args = argparse.Namespace(
        vaults_cmd="audit-links",
        vault=str(wiki),
        limit=5,
        scope="all",
        json=False,
    )

    assert vaults.cmd_vaults(args) == 0
    assert "未发现内部 wikilink 断链" in capsys.readouterr().out


def test_vaults_repair_links_cli_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    from core.cli.commands import vaults

    wiki = tmp_path / "wiki"
    target = wiki / "Known.md"
    _write(target, "known")
    source = wiki / "L2.4-KG" / "Entity.md"
    _write(source, f"- [[{target.as_posix()}]]\n")
    monkeypatch.setattr(vaults, "_get_config", lambda: _FakeConfig(wiki))

    args = argparse.Namespace(
        vaults_cmd="repair-links",
        vault=None,
        limit=5,
        scope="kg",
        strip_broken=False,
        apply=False,
        allow_dirty=False,
        json=False,
    )

    assert vaults.cmd_vaults(args) == 0
    assert "dry-run 未写入" in capsys.readouterr().out
    assert source.read_text(encoding="utf-8") == f"- [[{target.as_posix()}]]\n"


def test_vaults_repair_links_with_explicit_vault_does_not_read_config(
    tmp_path, monkeypatch, capsys
):
    from core.cli.commands import vaults

    wiki = tmp_path / "wiki"
    target = wiki / "Known.md"
    _write(target, "known")
    source = wiki / "L2.4-KG" / "Entity.md"
    _write(source, f"- [[{target.as_posix()}]]\n")
    monkeypatch.setattr(
        vaults,
        "_get_config",
        lambda: (_ for _ in ()).throw(AssertionError("config should not load")),
    )

    args = argparse.Namespace(
        vaults_cmd="repair-links",
        vault=str(wiki),
        limit=5,
        scope="kg",
        strip_broken=False,
        apply=False,
        allow_dirty=False,
        json=False,
    )

    assert vaults.cmd_vaults(args) == 0
    assert "候选链接: 1" in capsys.readouterr().out
