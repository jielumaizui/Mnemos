import json

import pytest

from core.access_policy import ACLReconciler, WikiProjectionBatchReceipt
from core.frontmatter import parse_frontmatter


def _write_page(path, frontmatter, body="body"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}\n", encoding="utf-8")


def _reconciler(wiki, raw):
    def commit(_backup_dir, update_count):
        return WikiProjectionBatchReceipt(
            update_count=update_count,
            mutation_count=update_count,
            event_count=update_count,
            backup_manifest="test:lifecycle-event-batch",
            source="test_acl_projection",
        )

    return ACLReconciler(
        wiki_dir=wiki,
        raw_dir=raw,
        wiki_projection_commit=commit,
    )


def test_acl_reconciliation_backfills_provenance_and_restricts_unknown(tmp_path):
    wiki = tmp_path / "wiki"
    raw = tmp_path / "raw"
    proven_wiki = wiki / "03-Tech" / "proven.md"
    unknown_wiki = wiki / "04-Concepts" / "unknown.md"
    unbacked_wiki = wiki / "03-Tech" / "unbacked.md"
    proven_raw = raw / "2026" / "session.md"
    _write_page(
        proven_wiki,
        "来源: claude\n来源会话: session-2\n项目: mnemos\n",
    )
    _write_page(unknown_wiki, "名称: Unknown legacy page\n")
    _write_page(
        unbacked_wiki,
        "来源: codex\n来源会话: missing-session\n项目: mnemos\n",
    )
    _write_page(
        proven_raw,
        "source: claude\nsession_id: session-2\nproject: mnemos\n",
    )
    reconciler = _reconciler(wiki, raw)

    dry_run = reconciler.reconcile(apply=False)

    assert dry_run == {
        "total": 4,
        "would_change": 4,
        "changed": 0,
        "proven": 2,
        "restricted": 2,
        "parse_errors": 0,
        "unresolved": 0,
    }
    assert "acl_metadata_complete" not in proven_wiki.read_text(encoding="utf-8")

    applied = reconciler.reconcile(
        apply=True,
        targets=("wiki", "raw"),
        backup_dir=tmp_path / "backup-1",
    )
    assert applied["changed"] == 4
    assert applied["unresolved"] == 0

    proven_fm, _ = parse_frontmatter(proven_wiki.read_text(encoding="utf-8"))
    assert proven_fm["scope"] == "private"
    assert proven_fm["source_agent"] == "claude"
    assert proven_fm["session_id"] == "session-2"
    assert proven_fm["project"] == "mnemos"
    assert proven_fm["acl_metadata_complete"] is True

    raw_fm, _ = parse_frontmatter(proven_raw.read_text(encoding="utf-8"))
    assert raw_fm["scope"] == "private"
    assert raw_fm["source_agent"] == "claude"
    assert raw_fm["acl_metadata_complete"] is True

    unknown_fm, _ = parse_frontmatter(unknown_wiki.read_text(encoding="utf-8"))
    assert unknown_fm["scope"] == "restricted"
    assert unknown_fm["acl_reconciliation_status"] == "restricted_unknown"
    assert unknown_fm["acl_metadata_complete"] is True

    unbacked_fm, _ = parse_frontmatter(unbacked_wiki.read_text(encoding="utf-8"))
    assert unbacked_fm["scope"] == "restricted"
    assert unbacked_fm["acl_reconciliation_status"] == "restricted_unknown"

    rerun = reconciler.reconcile(
        apply=True,
        targets=("wiki", "raw"),
        backup_dir=tmp_path / "backup-2",
    )
    assert rerun["would_change"] == 0
    assert rerun["changed"] == 0


def test_reconciliation_downgrades_unproven_shared_scopes_to_private(tmp_path):
    wiki = tmp_path / "wiki"
    raw = tmp_path / "raw"
    project_spoof = wiki / "project-spoof.md"
    global_spoof = wiki / "global-spoof.md"
    agent_spoof = wiki / "agent-spoof.md"
    raw_page = raw / "session.md"
    _write_page(
        raw_page,
        "source: codex\nsession_id: session-1\nproject: secret\n",
    )
    _write_page(
        project_spoof,
        "source: codex\nsession_id: session-1\nscope: project\nproject: public\n",
    )
    _write_page(
        global_spoof,
        "source: codex\nsession_id: session-1\nscope: global\n",
    )
    _write_page(
        agent_spoof,
        "source: codex\nsession_id: session-1\nscope: agent\n",
    )

    _reconciler(wiki, raw).reconcile(
        apply=True,
        targets=("wiki",),
        backup_dir=tmp_path / "backup",
    )

    project_fm, _ = parse_frontmatter(project_spoof.read_text(encoding="utf-8"))
    global_fm, _ = parse_frontmatter(global_spoof.read_text(encoding="utf-8"))
    agent_fm, _ = parse_frontmatter(agent_spoof.read_text(encoding="utf-8"))
    assert project_fm["scope"] == "private"
    assert project_fm["acl_reconciliation_status"] == "proven"
    assert global_fm["scope"] == "private"
    assert agent_fm["scope"] == "private"


def test_reconciliation_preserves_complete_server_principal_scope(tmp_path):
    wiki = tmp_path / "wiki"
    raw = tmp_path / "raw"
    page = wiki / "server-principal.md"
    _write_page(
        page,
        """scope: project
source_agent: codex
project: mnemos
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: server_principal
""",
    )

    report = ACLReconciler(wiki_dir=wiki, raw_dir=raw).reconcile(
        apply=True,
        targets=("wiki",),
        backup_dir=tmp_path / "backup",
    )

    frontmatter, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert report["would_change"] == 0
    assert frontmatter["scope"] == "project"
    assert frontmatter["acl_reconciliation_status"] == "server_principal"


def test_reconciliation_downgrades_prior_proven_shared_scope_to_private(tmp_path):
    wiki = tmp_path / "wiki"
    raw = tmp_path / "raw"
    page = wiki / "prior-proven.md"
    raw_page = raw / "session.md"
    _write_page(
        raw_page,
        "source: codex\nsession_id: session-1\nproject: mnemos\n",
    )
    _write_page(
        page,
        """scope: agent
source_agent: codex
session_id: session-1
project: mnemos
acl_schema_version: 1
acl_metadata_complete: true
acl_reconciliation_status: proven
""",
    )

    _reconciler(wiki, raw).reconcile(
        apply=True,
        targets=("wiki",),
        backup_dir=tmp_path / "backup",
    )

    frontmatter, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert frontmatter["scope"] == "private"
    assert frontmatter["acl_reconciliation_status"] == "proven"


def test_reconciliation_reports_malformed_yaml_as_unresolved(tmp_path):
    wiki = tmp_path / "wiki"
    raw = tmp_path / "raw"
    malformed = wiki / "malformed.md"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("---\ntags: [broken\n---\nbody\n", encoding="utf-8")

    report = ACLReconciler(wiki_dir=wiki, raw_dir=raw).reconcile(apply=False)

    assert report["parse_errors"] == 1
    assert report["unresolved"] == 1
    assert report["would_change"] == 0


def test_reconciliation_can_backup_and_limit_apply_to_wiki(tmp_path):
    wiki = tmp_path / "wiki"
    raw = tmp_path / "raw"
    backup = tmp_path / "backup"
    wiki_page = wiki / "page.md"
    raw_page = raw / "session.md"
    _write_page(wiki_page, "名称: Wiki page\n")
    _write_page(raw_page, "source: codex\nsession_id: session-1\nproject: mnemos\n")
    original_wiki = wiki_page.read_bytes()
    original_raw = raw_page.read_bytes()

    report = _reconciler(wiki, raw).reconcile(
        apply=True,
        targets=("wiki",),
        backup_dir=backup,
    )

    assert report["total"] == 1
    assert report["changed"] == 1
    assert (backup / "wiki" / "page.md").read_bytes() == original_wiki
    assert raw_page.read_bytes() == original_raw
    assert "acl_metadata_complete: true" in wiki_page.read_text(encoding="utf-8")
    manifest = json.loads((backup / "acl-reconciliation-manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "committed"
    assert manifest["files"][0]["relative_path"] == "page.md"


def test_acl_apply_requires_explicit_targets_and_backup(tmp_path):
    reconciler = ACLReconciler(wiki_dir=tmp_path / "wiki", raw_dir=tmp_path / "raw")

    with pytest.raises(ValueError, match="explicit wiki/raw targets"):
        reconciler.reconcile(apply=True, backup_dir=tmp_path / "backup")
    with pytest.raises(ValueError, match="recovery backup directory"):
        reconciler.reconcile(apply=True, targets=("wiki",))

    wiki_page = tmp_path / "wiki" / "page.md"
    _write_page(wiki_page, "title: legacy\n")
    with pytest.raises(ValueError, match="lifecycle/event projection committer"):
        reconciler.reconcile(
            apply=True,
            targets=("wiki",),
            backup_dir=tmp_path / "wiki-backup",
        )


def test_acl_apply_refuses_unresolved_plan_without_writing(tmp_path):
    wiki = tmp_path / "wiki"
    valid = wiki / "valid.md"
    malformed = wiki / "malformed.md"
    _write_page(valid, "名称: valid\n")
    malformed.write_text("---\ntags: [broken\n---\nbody\n", encoding="utf-8")
    original = valid.read_bytes()
    backup = tmp_path / "backup"

    with pytest.raises(ValueError, match="unresolved"):
        ACLReconciler(wiki_dir=wiki, raw_dir=tmp_path / "raw").reconcile(
            apply=True,
            targets=("wiki",),
            backup_dir=backup,
        )

    assert valid.read_bytes() == original
    assert not backup.exists()


def test_acl_batch_failure_restores_every_attempted_source(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    first = wiki / "first.md"
    second = wiki / "second.md"
    _write_page(first, "名称: first\n")
    _write_page(second, "名称: second\n")
    originals = {first: first.read_bytes(), second: second.read_bytes()}
    backup = tmp_path / "backup"
    reconciler = _reconciler(wiki, tmp_path / "raw")
    original_writer = reconciler._atomic_write_text
    failed = False

    def fail_second_once(path, content):
        nonlocal failed
        if path == second and not failed:
            failed = True
            raise OSError("injected second write failure")
        original_writer(path, content)

    monkeypatch.setattr(reconciler, "_atomic_write_text", fail_second_once)

    with pytest.raises(RuntimeError, match="all attempted files restored"):
        reconciler.reconcile(
            apply=True,
            targets=("wiki",),
            backup_dir=backup,
        )

    assert first.read_bytes() == originals[first]
    assert second.read_bytes() == originals[second]
    assert (backup / "wiki" / "first.md").read_bytes() == originals[first]
    assert (backup / "wiki" / "second.md").read_bytes() == originals[second]
    manifest = json.loads((backup / "acl-reconciliation-manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "rolled_back"


def test_acl_projection_failure_restores_materialized_wiki_batch(tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "page.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"---\r\ntitle: legacy\r\n---\r\n\r\nBody without trailing newline")
    original = page.read_bytes()

    def fail_projection(_backup_dir, _update_count):
        raise RuntimeError("synthetic projection failure after file writes")

    backup = tmp_path / "backup"
    with pytest.raises(RuntimeError, match="all attempted files restored"):
        ACLReconciler(
            wiki_dir=wiki,
            raw_dir=tmp_path / "raw",
            wiki_projection_commit=fail_projection,
        ).reconcile(
            apply=True,
            targets=("wiki",),
            backup_dir=backup,
        )

    assert page.read_bytes() == original
    manifest = json.loads((backup / "acl-reconciliation-manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "rolled_back"
