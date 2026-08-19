from __future__ import annotations

from pathlib import Path

from scripts import audit_hardcoded_paths as audit

MACHINE_PATH = "/" + "Users" + "/zhuwei/mnemos"
LEGACY_WIKI_PATH = "Documents" + "/Obsidian Vault/wiki"
RAW_VAULT_LITERAL = "~" + "/Documents/raw"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scan_flags_machine_and_legacy_vault_paths(tmp_path: Path) -> None:
    _write(
        tmp_path / "core" / "bad_paths.py",
        "\n".join(
            [
                f'PROJECT = Path("{MACHINE_PATH}")',
                f'WIKI = "{LEGACY_WIKI_PATH}"',
                f'RAW = "{RAW_VAULT_LITERAL}"',
            ]
        ),
    )

    findings = audit.scan_project(tmp_path, ["core"])

    assert {finding.rule for finding in findings} == {
        "machine_absolute_path",
        "legacy_obsidian_wiki_default",
        "documents_vault_literal",
    }


def test_scan_allows_config_authority_default_vaults(tmp_path: Path) -> None:
    _write(
        tmp_path / "core" / "config.py",
        "\n".join(
            [
                'return Path.home() / "Documents" / "mnemos"',
                'return Path.home() / "Documents" / "raw"',
            ]
        ),
    )

    assert audit.scan_project(tmp_path, ["core"]) == []


def test_main_strict_returns_nonzero_for_findings(monkeypatch, tmp_path: Path) -> None:
    _write(tmp_path / "core" / "bad_paths.py", f'PROJECT = "{MACHINE_PATH}"')
    monkeypatch.setattr(audit, "PROJECT_ROOT", tmp_path)

    assert audit.main(["--strict", "--target", "core"]) == 1
