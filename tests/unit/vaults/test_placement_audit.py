from __future__ import annotations

import argparse
from pathlib import Path


def test_audit_vault_placement_reports_duplicate_names_and_kg_collisions(tmp_path: Path):
    from core.vaults.placement_audit import audit_vault_placement

    (tmp_path / "03-Tech" / "python").mkdir(parents=True)
    (tmp_path / "04-Concepts").mkdir()
    (tmp_path / "L2.4-KG" / "Entities").mkdir(parents=True)

    (tmp_path / "03-Tech" / "python-pptx.md").write_text("# python-pptx\n", encoding="utf-8")
    (tmp_path / "03-Tech" / "python" / "python-pptx.md").write_text(
        "# python-pptx\n\nDifferent body\n",
        encoding="utf-8",
    )
    (tmp_path / "04-Concepts" / "uv.md").write_text("# uv\n", encoding="utf-8")
    (tmp_path / "L2.4-KG" / "Entities" / "uv.md").write_text("# uv projection\n", encoding="utf-8")

    report = audit_vault_placement(tmp_path)

    assert report["markdown_files"] == 4
    assert report["duplicate_basename_groups"] == 2
    assert report["duplicate_basename_files"] == 4
    assert report["kg_entity_collision_count"] == 1
    assert report["kg_entity_collisions"][0]["name"] == "uv"
    assert report["folders"]["03-Tech"]["subtree_duplicate_groups"] == 1


def test_vaults_audit_placement_cli_prints_summary(tmp_path: Path, monkeypatch, capsys):
    from core.cli.commands import vaults

    (tmp_path / "03-Tech").mkdir()
    (tmp_path / "04-Concepts").mkdir()
    (tmp_path / "03-Tech" / "same.md").write_text("# same\n", encoding="utf-8")
    (tmp_path / "04-Concepts" / "same.md").write_text("# same\n", encoding="utf-8")

    class FakeConfig:
        wiki_dir = tmp_path

        def vault_dir(self, name: str) -> Path:
            assert name == "mnemos"
            return tmp_path

    monkeypatch.setattr(vaults, "_get_config", lambda: FakeConfig())

    args = argparse.Namespace(vaults_cmd="audit-placement", json=False)
    assert vaults.cmd_vaults(args) == 0

    captured = capsys.readouterr()
    assert "Vault placement audit" in captured.out
    assert "duplicate_basename_groups: 1" in captured.out


def test_repair_identical_duplicate_basenames_archives_only_redundant_files(tmp_path: Path):
    from core.vaults.placement_audit import (
        audit_vault_placement,
        repair_identical_duplicate_basenames,
    )

    (tmp_path / "03-Tech").mkdir()
    (tmp_path / "04-Concepts").mkdir()
    (tmp_path / "03-Tech" / "same.md").write_text("# Same\n", encoding="utf-8")
    (tmp_path / "04-Concepts" / "same.md").write_text("# Same\n", encoding="utf-8")
    (tmp_path / "03-Tech" / "different.md").write_text("# One\n", encoding="utf-8")
    (tmp_path / "04-Concepts" / "different.md").write_text("# Two\n", encoding="utf-8")

    report = repair_identical_duplicate_basenames(
        tmp_path,
        apply=True,
        archive_date="2026-07-03",
    )

    assert report["status"] == "applied"
    assert report["planned_moves"] == 1
    assert report["moved"] == 1
    assert (tmp_path / "03-Tech" / "same.md").exists()
    assert not (tmp_path / "04-Concepts" / "same.md").exists()
    assert (tmp_path / "03-Tech" / "different.md").exists()
    assert (tmp_path / "04-Concepts" / "different.md").exists()

    archive_path = tmp_path / report["moves"][0]["archive_path"]
    assert archive_path.exists()
    assert archive_path.name.startswith("same__duplicate-")

    audit = audit_vault_placement(tmp_path)
    assert {group["name"] for group in audit["duplicate_basenames"]} == {"different"}


def test_vaults_repair_placement_cli_dry_run_prints_plan(tmp_path: Path, monkeypatch, capsys):
    from core.cli.commands import vaults

    (tmp_path / "03-Tech").mkdir()
    (tmp_path / "04-Concepts").mkdir()
    (tmp_path / "03-Tech" / "same.md").write_text("# Same\n", encoding="utf-8")
    (tmp_path / "04-Concepts" / "same.md").write_text("# Same\n", encoding="utf-8")

    class FakeConfig:
        def vault_dir(self, name: str) -> Path:
            assert name == "mnemos"
            return tmp_path

    monkeypatch.setattr(vaults, "_get_config", lambda: FakeConfig())

    args = argparse.Namespace(
        vaults_cmd="repair-placement",
        apply=False,
        allow_dirty=False,
        json=False,
        limit=None,
    )
    assert vaults.cmd_vaults(args) == 0

    captured = capsys.readouterr()
    assert "Vault placement repair dry-run" in captured.out
    assert "planned_moves: 1" in captured.out
    assert (tmp_path / "04-Concepts" / "same.md").exists()


def test_vaults_repair_placement_apply_refuses_dirty_vault(
    tmp_path: Path, monkeypatch, capsys
):
    from core.cli.commands import vaults

    class FakeConfig:
        def vault_dir(self, name: str) -> Path:
            assert name == "mnemos"
            return tmp_path

    monkeypatch.setattr(vaults, "_get_config", lambda: FakeConfig())
    monkeypatch.setattr(vaults, "_vault_git_dirty", lambda _vault_dir: " M page.md")

    args = argparse.Namespace(
        vaults_cmd="repair-placement",
        apply=True,
        allow_dirty=False,
        json=False,
        limit=None,
    )
    assert vaults.cmd_vaults(args) == 2

    captured = capsys.readouterr()
    assert "dirty" in captured.out
    assert "--allow-dirty" in captured.out
