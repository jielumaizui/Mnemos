from __future__ import annotations

import shutil
import sqlite3

import pytest

from core.frontmatter import parse_frontmatter, write_frontmatter
from core.mnemos_bus import EventBus
from core.wiki_projection_lifecycle import WikiProjectionLedger
from scripts import reconcile_wiki_acl_projection as module


def _add_restricted_acl(path):
    frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    frontmatter.update(
        {
            "scope": "restricted",
            "acl_schema_version": 1,
            "acl_metadata_complete": True,
            "acl_reconciliation_status": "restricted_unknown",
        }
    )
    path.write_text(write_frontmatter(frontmatter, body), encoding="utf-8")


def _prepare(tmp_path, patched_get_config):
    wiki = tmp_path / "wiki"
    database = tmp_path / "database"
    acl_backup = tmp_path / "acl-backup" / "wiki"
    wiki.mkdir()
    database.mkdir()
    normal = wiki / "normal.md"
    normal.write_text("---\ntitle: Normal\n---\n\n# Normal\n\nStable body.\n")
    system_dir = wiki / "L3-Observations"
    system_dir.mkdir()
    system = system_dir / "attention.md"
    system.write_text("---\ndimension: attention\n---\n\n# Attention\n\nOld body.\n")
    ledger = WikiProjectionLedger(database / "wiki_projection.db")
    ledger.record_mutation(normal, mutation_type="create")
    ledger.record_mutation(system, mutation_type="create")

    system.write_text("---\ndimension: attention\n---\n\n# Attention\n\nNew body.\n")
    shutil.copytree(wiki, acl_backup)
    _add_restricted_acl(normal)
    _add_restricted_acl(system)

    patched_get_config.wiki_dir = wiki
    patched_get_config.database_dir = database
    patched_get_config.mnemos_dir = database
    patched_get_config.data_dir = database
    bus = EventBus(
        config=patched_get_config,
        run_startup_maintenance=False,
        recover_pending=False,
    )
    bus.close()
    return wiki, database, acl_backup


def test_apply_records_exact_mutations_and_pending_events(
    tmp_path, patched_get_config, monkeypatch
):
    _wiki, database, acl_backup = _prepare(tmp_path, patched_get_config)
    monkeypatch.setattr(module, "get_config", lambda: patched_get_config)
    monkeypatch.setattr(module, "runtime_writers_are_inactive", lambda _path: True)
    dry = module.reconcile(apply=False, acl_backup_dir=acl_backup)

    assert dry["ok"] is True
    assert dry["plan"]["needs_mutation_count"] == 2
    assert dry["plan"]["classification_counts"] == {
        "acl_backup_matches_ledger": 1,
        "preexisting_drift_unmatched": 1,
    }
    backup = tmp_path / "projection-backup"
    result = module.reconcile(
        apply=True,
        acl_backup_dir=acl_backup,
        backup_dir=backup,
        reviewed_plan_hash=dry["plan"]["plan_hash"],
    )

    assert result["ok"] is True
    diagnostics = result["diagnostics"]
    assert diagnostics["scan"]["recorded_mutations"] == 2
    assert diagnostics["event_validation"] == {
        "expected": 2,
        "found": 2,
        "mismatch": 0,
    }
    assert diagnostics["after_plan"]["needs_mutation_count"] == 0
    with sqlite3.connect(database / "wiki_projection.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM wiki_mutations").fetchone()[0] == 4
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM wiki_mutations WHERE event_trace_id=mutation_id"
            ).fetchone()[0]
            == 2
        )
    with sqlite3.connect(database / "events.db") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE source=? AND status='pending'",
                ("cog015_acl_projection_reconciliation",),
            ).fetchone()[0]
            == 2
        )


def test_publish_failure_restores_both_sqlite_snapshots(tmp_path, patched_get_config, monkeypatch):
    _wiki, database, acl_backup = _prepare(tmp_path, patched_get_config)
    monkeypatch.setattr(module, "get_config", lambda: patched_get_config)
    monkeypatch.setattr(module, "runtime_writers_are_inactive", lambda _path: True)
    dry = module.reconcile(apply=False, acl_backup_dir=acl_backup)
    before_ledger = module._sqlite_snapshot(database / "wiki_projection.db")
    before_events = module._sqlite_snapshot(database / "events.db")
    original_publish = module.publish_wiki_mutation
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic publish failure")
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(module, "publish_wiki_mutation", fail_second)
    backup = tmp_path / "rollback-backup"

    with pytest.raises(RuntimeError, match="synthetic publish failure"):
        module.reconcile(
            apply=True,
            acl_backup_dir=acl_backup,
            backup_dir=backup,
            reviewed_plan_hash=dry["plan"]["plan_hash"],
        )

    assert module._sqlite_snapshot(database / "wiki_projection.db") == before_ledger
    assert module._sqlite_snapshot(database / "events.db") == before_events
    manifest = module.json.loads(
        (backup / "wiki-acl-projection-reconciliation-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "rolled_back"


def test_exact_batch_commits_only_reviewed_paths(
    tmp_path,
    patched_get_config,
    monkeypatch,
):
    wiki = tmp_path / "wiki"
    database = tmp_path / "database"
    wiki.mkdir()
    database.mkdir()
    changed = wiki / "changed.md"
    unchanged = wiki / "unchanged.md"
    changed.write_text("---\ntitle: Changed\n---\n\nBefore.\n", encoding="utf-8")
    unchanged.write_text("---\ntitle: Stable\n---\n\nStable.\n", encoding="utf-8")
    ledger = WikiProjectionLedger(database / "wiki_projection.db")
    ledger.record_mutation(changed, mutation_type="create")
    ledger.record_mutation(unchanged, mutation_type="create")
    before = module._sha256_path(changed)
    changed.write_text("---\ntitle: Changed\n---\n\nAfter.\n", encoding="utf-8")
    after = module._sha256_path(changed)
    patched_get_config.wiki_dir = wiki
    patched_get_config.database_dir = database
    patched_get_config.mnemos_dir = database
    patched_get_config.data_dir = database
    monkeypatch.setattr(module, "runtime_writers_are_inactive", lambda _path: True)

    result = module.commit_exact_wiki_updates(
        config=patched_get_config,
        updates=[
            module.ExactWikiProjectionUpdate(
                path=changed,
                before_sha256=before,
                after_sha256=after,
            )
        ],
        backup_dir=tmp_path / "backup",
        source="test_exact_wiki_batch",
    )

    assert result["ok"] is True
    assert result["diagnostics"]["recorded_mutations"] == 1
    assert result["diagnostics"]["published_events"] == 1
    with sqlite3.connect(database / "wiki_projection.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM wiki_mutations").fetchone()[0] == 3
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM wiki_mutations WHERE page_path=?",
                (str(unchanged.resolve()),),
            ).fetchone()[0]
            == 1
        )


def test_exact_batch_publish_failure_restores_both_databases(
    tmp_path,
    patched_get_config,
    monkeypatch,
):
    wiki = tmp_path / "wiki"
    database = tmp_path / "database"
    wiki.mkdir()
    database.mkdir()
    page = wiki / "page.md"
    page.write_text("---\ntitle: Page\n---\n\nBefore.\n", encoding="utf-8")
    WikiProjectionLedger(database / "wiki_projection.db").record_mutation(
        page,
        mutation_type="create",
    )
    before_hash = module._sha256_path(page)
    before_ledger = module._sqlite_snapshot(database / "wiki_projection.db")
    page.write_text("---\ntitle: Page\n---\n\nAfter.\n", encoding="utf-8")
    patched_get_config.wiki_dir = wiki
    patched_get_config.database_dir = database
    patched_get_config.mnemos_dir = database
    patched_get_config.data_dir = database
    monkeypatch.setattr(module, "runtime_writers_are_inactive", lambda _path: True)
    monkeypatch.setattr(
        module,
        "publish_wiki_mutation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic exact-batch publish failure")
        ),
    )

    with pytest.raises(RuntimeError, match="synthetic exact-batch publish failure"):
        module.commit_exact_wiki_updates(
            config=patched_get_config,
            updates=[
                module.ExactWikiProjectionUpdate(
                    path=page,
                    before_sha256=before_hash,
                    after_sha256=module._sha256_path(page),
                )
            ],
            backup_dir=tmp_path / "rollback-backup",
            source="test_exact_wiki_batch",
        )

    assert module._sqlite_snapshot(database / "wiki_projection.db") == before_ledger
    assert not (database / "events.db").exists()
    manifest = module.json.loads(
        (tmp_path / "rollback-backup" / "wiki-projection-batch-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["status"] == "rolled_back"
