from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.frontmatter import read_frontmatter_only, write_frontmatter
from core.ops.exclusive_file_lock import ExclusiveFileLockError, exclusive_file_lock
from core.wiki_projection_lifecycle import WikiProjectionLedger
from scripts import reconcile_entropy_report_frontmatter as module
from scripts.reconcile_entropy_report_frontmatter import (
    reconcile_entropy_reports,
    recover_entropy_reconciliation,
)
from scripts import reconcile_wiki_acl_projection as projection_module


def test_entropy_reconcile_compacts_frontmatter_with_backup_and_is_idempotent(
    tmp_path,
    patched_get_config,
):
    wiki = tmp_path / "wiki"
    page = wiki / "06-Retrospectives" / "entropy" / "entropy-suggestions-2026-07-20-deadbeef00.md"
    page.parent.mkdir(parents=True)
    frontmatter = {
        "report_type": "entropy_suggestions",
        "source_db": "/tmp/entropy.db",
        "source_payload_digest": "d" * 64,
        "source_row_ids": list(range(1, 5001)),
        "sources": [f"sqlite:/tmp/entropy.db#entropy_suggestions/{i}" for i in range(1, 5001)],
    }
    body = "# Entropy report\n\nExact report body remains unchanged.\n"
    page.write_text(write_frontmatter(frontmatter, body), encoding="utf-8")
    original = page.read_bytes()
    database = tmp_path / "database"
    database.mkdir()
    patched_get_config.wiki_dir = wiki
    patched_get_config.database_dir = database
    patched_get_config.mnemos_dir = database
    patched_get_config.data_dir = database
    WikiProjectionLedger(database / "wiki_projection.db").record_mutation(
        page,
        mutation_type="create",
    )

    preview = reconcile_entropy_reports(wiki)
    backup = tmp_path / "backup"
    applied = reconcile_entropy_reports(
        wiki,
        apply=True,
        backup_dir=backup,
        projection_config=patched_get_config,
        daemon_check=lambda _path: True,
    )

    assert preview["would_change"] == 1
    assert preview["oversized_before_count"] == 1
    assert applied["changed"] == 1
    assert (
        backup
        / "wiki"
        / "06-Retrospectives"
        / "entropy"
        / "entropy-suggestions-2026-07-20-deadbeef00.md"
    ).read_bytes() == original
    compacted = read_frontmatter_only(page)
    assert compacted["source_row_id_count"] == 5000
    assert "source_row_ids" not in compacted
    assert "sources" not in compacted
    assert page.read_text(encoding="utf-8").endswith(body)
    assert applied["wiki_projection"]["diagnostics"]["recorded_mutations"] == 1
    assert applied["wiki_projection"]["diagnostics"]["published_events"] == 1
    assert reconcile_entropy_reports(wiki)["would_change"] == 0


def test_entropy_projection_failure_restores_exact_markdown_preimage(
    tmp_path,
    patched_get_config,
    monkeypatch,
):
    wiki = tmp_path / "wiki"
    page = wiki / "06-Retrospectives" / "entropy" / "entropy-suggestions-failure.md"
    page.parent.mkdir(parents=True)
    rendered = write_frontmatter(
        {
            "report_type": "entropy_suggestions",
            "source_db": "/tmp/entropy.db",
            "source_payload_digest": "d" * 64,
            "source_row_ids": [1, 2, 3],
            "sources": ["sqlite:/tmp/entropy.db#entropy_suggestions/1"],
        },
        "# Exact body",
    )
    page.write_bytes(rendered.replace("\n", "\r\n").encode("utf-8"))
    original = page.read_bytes()
    database = tmp_path / "database"
    database.mkdir()
    patched_get_config.wiki_dir = wiki
    patched_get_config.database_dir = database
    patched_get_config.mnemos_dir = database
    patched_get_config.data_dir = database
    WikiProjectionLedger(database / "wiki_projection.db").record_mutation(
        page,
        mutation_type="create",
    )
    monkeypatch.setattr(
        module,
        "commit_exact_wiki_updates",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic lifecycle failure")),
    )
    backup = tmp_path / "backup"

    with pytest.raises(RuntimeError, match="all files restored"):
        reconcile_entropy_reports(
            wiki,
            apply=True,
            backup_dir=backup,
            projection_config=patched_get_config,
            daemon_check=lambda _path: True,
        )

    assert page.read_bytes() == original
    manifest = json.loads(
        (backup / "entropy-frontmatter-reconciliation-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "rolled_back"


def test_entropy_apply_loses_daemon_start_race_before_markdown_write(
    tmp_path,
    patched_get_config,
):
    wiki = tmp_path / "wiki-race"
    page = wiki / "06-Retrospectives" / "entropy" / "entropy-suggestions-race.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        write_frontmatter(
            {
                "report_type": "entropy_suggestions",
                "source_row_ids": [1, 2, 3],
                "sources": ["sqlite:fixture#1"],
            },
            "# Race body\n",
        ),
        encoding="utf-8",
    )
    original = page.read_bytes()
    database = tmp_path / "database-race"
    database.mkdir()
    patched_get_config.wiki_dir = wiki
    patched_get_config.database_dir = database

    with exclusive_file_lock(
        database / "daemon.pid",
        unavailable_message="test lock",
    ):
        with pytest.raises(ExclusiveFileLockError, match="daemon started"):
            reconcile_entropy_reports(
                wiki,
                apply=True,
                backup_dir=tmp_path / "backup-race",
                projection_config=patched_get_config,
                daemon_check=lambda _path: True,
            )

    assert page.read_bytes() == original
    assert not (tmp_path / "backup-race").exists()


def test_entropy_prepared_manifest_recovers_markdown_and_projection_databases(
    tmp_path,
    patched_get_config,
):
    wiki = tmp_path / "wiki-recovery"
    page = wiki / "06-Retrospectives" / "entropy" / "entropy-suggestions-recovery.md"
    page.parent.mkdir(parents=True)
    rendered = write_frontmatter(
        {
            "report_type": "entropy_suggestions",
            "source_row_ids": [1, 2, 3],
            "sources": ["sqlite:fixture#1"],
        },
        "# Recovery body without final newline",
    )
    page.write_bytes(rendered.replace("\n", "\r\n").encode("utf-8"))
    original = page.read_bytes()
    database = tmp_path / "database-recovery"
    database.mkdir()
    patched_get_config.wiki_dir = wiki
    patched_get_config.database_dir = database
    patched_get_config.mnemos_dir = database
    patched_get_config.data_dir = database
    WikiProjectionLedger(database / "wiki_projection.db").record_mutation(
        page,
        mutation_type="create",
    )
    ledger_before = projection_module._sqlite_snapshot(database / "wiki_projection.db")
    backup = tmp_path / "backup-recovery"
    reconcile_entropy_reports(
        wiki,
        apply=True,
        backup_dir=backup,
        projection_config=patched_get_config,
        daemon_check=lambda _path: True,
    )
    manifest_path = backup / "entropy-frontmatter-reconciliation-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "committed"
    manifest["status"] = "prepared"
    module.ACLReconciler._atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    recovered = recover_entropy_reconciliation(
        wiki,
        backup_dir=backup,
        projection_config=patched_get_config,
        daemon_check=lambda _path: True,
    )

    assert recovered["status"] == "recovered_rollback"
    assert recovered["restored_file_count"] == 1
    assert page.read_bytes() == original
    assert projection_module._sqlite_snapshot(database / "wiki_projection.db") == ledger_before
    assert not (database / "events.db").exists()
    nested_manifest = json.loads(
        (backup / "wiki-projection" / "wiki-projection-batch-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert nested_manifest["status"] == "recovered_rollback"


def test_entropy_recovery_handles_nested_backup_before_manifest_window(
    tmp_path,
    patched_get_config,
):
    wiki = tmp_path / "wiki-prepared-window"
    relative = Path("06-Retrospectives/entropy/entropy-suggestions-prepared-window.md")
    page = wiki / relative
    page.parent.mkdir(parents=True)
    original = b"original entropy report\n"
    page.write_bytes(b"changed during interrupted reconciliation\n")

    database = tmp_path / "database-prepared-window"
    database.mkdir()
    patched_get_config.database_dir = database
    backup = tmp_path / "backup-prepared-window"
    backup_page = backup / "wiki" / relative
    backup_page.parent.mkdir(parents=True)
    backup_page.write_bytes(original)
    manifest = {
        "schema_version": "mnemos.entropy_report_frontmatter_reconcile_manifest.v1",
        "status": "prepared",
        "files": [
            {
                "relative_path": relative.as_posix(),
                "original_sha256": module._sha256_bytes(original),
                "desired_sha256": module._sha256_bytes(page.read_bytes()),
            }
        ],
    }
    module._write_manifest(
        backup / "entropy-frontmatter-reconciliation-manifest.json",
        manifest,
    )
    nested = backup / "wiki-projection"
    nested.mkdir()
    (nested / "wiki_projection.db").write_bytes(b"partial backup before manifest")

    recovered = recover_entropy_reconciliation(
        wiki,
        backup_dir=backup,
        projection_config=patched_get_config,
        daemon_check=lambda _path: True,
    )

    assert recovered["status"] == "recovered_rollback"
    assert recovered["wiki_projection"] == {
        "found": True,
        "status": "backup_preparing_no_database_mutation",
    }
    assert page.read_bytes() == original
