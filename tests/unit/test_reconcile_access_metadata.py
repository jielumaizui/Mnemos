from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from core.wiki_projection_lifecycle import WikiProjectionLedger
from core.ops.exclusive_file_lock import exclusive_file_lock
from scripts import reconcile_access_metadata as cli
from scripts import reconcile_wiki_acl_projection as projection_module


def _config(tmp_path):
    database = tmp_path / "database"
    return SimpleNamespace(
        wiki_dir=tmp_path / "wiki",
        obsidian_vault_path=tmp_path / "raw",
        database_dir=database,
        mnemos_dir=database,
        data_dir=database,
        get=lambda _key, default=None: default,
    )


def test_acl_cli_apply_requires_explicit_target(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "reconcile_access_metadata.py",
            "--apply",
            "--backup-dir",
            str(tmp_path / "backup"),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2


def test_acl_cli_apply_refuses_active_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "reconcile_access_metadata.py",
            "--apply",
            "--target",
            "wiki",
            "--backup-dir",
            str(tmp_path / "backup"),
        ],
    )
    monkeypatch.setattr(cli, "get_config", lambda: _config(tmp_path))
    monkeypatch.setattr(cli, "runtime_writers_are_inactive", lambda _path: False)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2


def test_acl_cli_dry_run_defaults_to_all_targets(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["reconcile_access_metadata.py"])
    monkeypatch.setattr(cli, "get_config", lambda: _config(tmp_path))

    assert cli.main() == 0

    output = capsys.readouterr().out
    assert '"mode": "dry_run"' in output
    assert '"target": "all"' in output


def test_acl_cli_apply_accepts_stopped_runtime_and_exact_target(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        "sys.argv",
        [
            "reconcile_access_metadata.py",
            "--apply",
            "--target",
            "wiki",
            "--backup-dir",
            str(tmp_path / "backup"),
        ],
    )
    monkeypatch.setattr(cli, "get_config", lambda: _config(tmp_path))
    monkeypatch.setattr(cli, "runtime_writers_are_inactive", lambda _path: True)

    assert cli.main() == 0

    output = capsys.readouterr().out
    assert '"mode": "apply"' in output
    assert '"target": "wiki"' in output


def test_acl_apply_loses_daemon_start_race_before_any_file_write(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.wiki_dir.mkdir()
    page = config.wiki_dir / "legacy.md"
    page.write_text("---\ntitle: Legacy\n---\n\nBody.\n", encoding="utf-8")
    original = page.read_bytes()
    monkeypatch.setattr(
        "sys.argv",
        [
            "reconcile_access_metadata.py",
            "--apply",
            "--target",
            "wiki",
            "--backup-dir",
            str(tmp_path / "backup-race"),
        ],
    )
    monkeypatch.setattr(cli, "get_config", lambda: config)
    monkeypatch.setattr(cli, "runtime_writers_are_inactive", lambda _path: True)

    with exclusive_file_lock(
        config.database_dir / "daemon.pid",
        unavailable_message="test lock",
    ):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 2
    assert page.read_bytes() == original
    assert not (tmp_path / "backup-race").exists()


def test_acl_cli_wiki_apply_commits_lifecycle_and_pending_event(
    tmp_path,
    monkeypatch,
    capsys,
):
    config = _config(tmp_path)
    config.wiki_dir.mkdir()
    config.database_dir.mkdir()
    page = config.wiki_dir / "legacy.md"
    unchanged = config.wiki_dir / "already-reconciled.md"
    page.write_text("---\ntitle: Legacy\n---\n\nBody.\n", encoding="utf-8")
    unchanged.write_text(
        "---\n"
        "title: Stable\n"
        "scope: restricted\n"
        "acl_schema_version: 1\n"
        "acl_metadata_complete: true\n"
        "acl_reconciliation_status: restricted_unknown\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    ledger = WikiProjectionLedger(config.database_dir / "wiki_projection.db")
    ledger.record_mutation(
        page,
        mutation_type="create",
    )
    ledger.record_mutation(unchanged, mutation_type="create")
    monkeypatch.setattr(
        "sys.argv",
        [
            "reconcile_access_metadata.py",
            "--apply",
            "--target",
            "wiki",
            "--backup-dir",
            str(tmp_path / "backup"),
        ],
    )
    monkeypatch.setattr(cli, "get_config", lambda: config)
    monkeypatch.setattr(cli, "runtime_writers_are_inactive", lambda _path: True)
    from scripts import reconcile_wiki_acl_projection as projection

    monkeypatch.setattr(projection, "runtime_writers_are_inactive", lambda _path: True)

    assert cli.main() == 0

    output = capsys.readouterr().out
    assert '"mutation_count": 1' in output
    assert '"event_count": 1' in output
    assert (
        tmp_path / "backup" / "wiki-projection" / "wiki-acl-projection-reconciliation-manifest.json"
    ).is_file()


def test_acl_process_death_after_projection_commit_recovers_exact_batch(tmp_path):
    config = _config(tmp_path)
    config.wiki_dir.mkdir()
    config.database_dir.mkdir()
    page = config.wiki_dir / "legacy.md"
    page.write_bytes(b"---\ntitle: Legacy\n---\n\nExact body without final newline.")
    original = page.read_bytes()
    WikiProjectionLedger(config.database_dir / "wiki_projection.db").record_mutation(
        page,
        mutation_type="create",
    )
    ledger_before = projection_module._sqlite_snapshot(
        config.database_dir / "wiki_projection.db"
    )
    backup = tmp_path / "crash-backup"
    project_root = Path(__file__).resolve().parents[2]
    child = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        import sys
        from types import SimpleNamespace

        from core.access_policy import ACLReconciler
        from scripts import reconcile_access_metadata as cli
        from scripts import reconcile_wiki_acl_projection as projection

        database = Path({str(config.database_dir)!r})
        config = SimpleNamespace(
            wiki_dir=Path({str(config.wiki_dir)!r}),
            obsidian_vault_path=Path({str(config.obsidian_vault_path)!r}),
            database_dir=database,
            mnemos_dir=database,
            data_dir=database,
            get=lambda _key, default=None: default,
        )
        cli.get_config = lambda: config
        cli.runtime_writers_are_inactive = lambda _path: True
        projection.runtime_writers_are_inactive = lambda _path: True
        original_write = ACLReconciler._atomic_write_text

        def terminate_before_top_commit(path, content):
            if (
                Path(path).name == "acl-reconciliation-manifest.json"
                and '"status": "committed"' in content
            ):
                os._exit(77)
            original_write(path, content)

        ACLReconciler._atomic_write_text = staticmethod(terminate_before_top_commit)
        sys.argv = [
            "reconcile_access_metadata.py",
            "--apply",
            "--target",
            "wiki",
            "--backup-dir",
            {str(backup)!r},
        ]
        cli.main()
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", child],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 77, completed.stderr
    assert page.read_bytes() != original
    top_manifest_path = backup / "acl-reconciliation-manifest.json"
    nested_manifest_path = (
        backup
        / "wiki-projection"
        / "wiki-acl-projection-reconciliation-manifest.json"
    )
    assert json.loads(top_manifest_path.read_text(encoding="utf-8"))["status"] == "prepared"
    assert json.loads(nested_manifest_path.read_text(encoding="utf-8"))["status"] == "committed"

    recovered = cli.recover_acl_reconciliation(
        config=config,
        backup_dir=backup,
        daemon_check=lambda _path: True,
    )

    assert recovered["status"] == "recovered_rollback"
    assert recovered["restored_file_count"] == 1
    assert page.read_bytes() == original
    assert (
        projection_module._sqlite_snapshot(config.database_dir / "wiki_projection.db")
        == ledger_before
    )
    assert not (config.database_dir / "events.db").exists()
    assert (
        json.loads(top_manifest_path.read_text(encoding="utf-8"))["status"]
        == "recovered_rollback"
    )
    assert (
        json.loads(nested_manifest_path.read_text(encoding="utf-8"))["status"]
        == "recovered_rollback"
    )


def test_acl_recovery_refuses_committed_top_level_manifest(tmp_path):
    config = _config(tmp_path)
    config.obsidian_vault_path.mkdir()
    page = config.obsidian_vault_path / "legacy.md"
    page.write_text("---\ntitle: Legacy\n---\n\nBody.\n", encoding="utf-8")
    original = page.read_bytes()
    backup = tmp_path / "committed-backup"
    reconciler = cli.ACLReconciler(
        wiki_dir=config.wiki_dir,
        raw_dir=config.obsidian_vault_path,
    )
    reconciler.reconcile(apply=True, targets=("raw",), backup_dir=backup)

    with pytest.raises(RuntimeError, match="refusing to recover a committed"):
        cli.recover_acl_reconciliation(
            config=config,
            backup_dir=backup,
            daemon_check=lambda _path: True,
        )

    assert page.read_bytes() != original
