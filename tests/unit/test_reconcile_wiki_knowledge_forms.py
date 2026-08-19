from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts.reconcile_wiki_knowledge_forms import (
    apply_updates,
    prepare_updates,
    recover_incomplete_generation,
)


def _page() -> str:
    return (
        "---\n"
        "名称: Review fixture\n"
        "领域: 测试\n"
        "摘要: 历史形态补写测试页面。\n"
        "来源会话: session-1\n"
        "蒸馏时间: '2026-07-23 00:00:00'\n"
        "---\n"
        "# Review fixture\n\n"
        "正文。\n"
    )


def _review(path, before_hash: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "mnemos.wiki_knowledge_form_review.v1",
                "entries": [
                    {
                        "relative_path": "review.md",
                        "before_sha256": before_hash,
                        "form": "洞察关联",
                        "evidence": "human-review:test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_prepare_updates_binds_plan_and_renders_canonical_form(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "review.md"
    page.write_text(_page(), encoding="utf-8")
    before_hash = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
    review = tmp_path / "review.json"
    _review(review, before_hash)

    plan, updates = prepare_updates(
        wiki_dir=wiki,
        checkpoint_db=None,
        review_manifest=review,
    )

    assert plan["apply_ready"] is True
    assert len(updates) == 1
    assert updates[0].before_sha256 == before_hash
    assert "知识形态: 洞察关联" in updates[0].content
    assert page.read_bytes() == _page().encode("utf-8")


def test_prepare_updates_refuses_plan_hash_drift(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "review.md"
    page.write_text(_page(), encoding="utf-8")
    review = tmp_path / "review.json"
    _review(review, "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest())

    with pytest.raises(RuntimeError, match="plan hash drifted"):
        prepare_updates(
            wiki_dir=wiki,
            checkpoint_db=None,
            review_manifest=review,
            expected_plan_hash="sha256:" + "0" * 64,
        )


def _prepared_apply(tmp_path):
    wiki = tmp_path / "wiki"
    database = tmp_path / "database"
    wiki.mkdir()
    database.mkdir()
    page = wiki / "review.md"
    page.write_text(_page(), encoding="utf-8")
    review = tmp_path / "review.json"
    _review(review, "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest())
    plan, updates = prepare_updates(
        wiki_dir=wiki,
        checkpoint_db=None,
        review_manifest=review,
    )
    config = SimpleNamespace(wiki_dir=wiki, database_dir=database)
    return config, page, plan, updates


def test_apply_requires_exact_hash_and_holds_lock_before_wiki_write(tmp_path):
    config, page, plan, updates = _prepared_apply(tmp_path)
    backup = tmp_path / "backup"

    with pytest.raises(ValueError, match="expected plan hash"):
        apply_updates(
            config=config,
            plan=plan,
            updates=updates,
            backup_dir=backup,
        )
    with pytest.raises(RuntimeError, match="stopped"):
        apply_updates(
            config=config,
            plan=plan,
            updates=updates,
            backup_dir=backup,
            expected_plan_hash=plan["plan_hash"],
            daemon_check=lambda _database_dir: False,
        )
    assert page.read_text(encoding="utf-8") == _page()
    assert not backup.exists()

    def projection_committer(**kwargs):
        committed = list(kwargs["updates"])
        assert len(committed) == 1
        assert "知识形态: 洞察关联" in page.read_text(encoding="utf-8")
        return {"ok": True, "diagnostics": {"update_count": 1}}

    report = apply_updates(
        config=config,
        plan=plan,
        updates=updates,
        backup_dir=backup,
        expected_plan_hash=plan["plan_hash"],
        daemon_check=lambda _database_dir: True,
        projection_committer=projection_committer,
    )
    assert report["ok"] is True
    assert report["apply_without_exact_plan_hash"] == 0
    assert report["wiki_write_before_offline_lock"] == 0
    assert report["partial_generation_visible"] == 0
    assert report["wiki_projection_generation_mismatch"] == 0


@pytest.mark.parametrize("failure_stage", ("after_wiki_write:1", "before_projection_commit"))
def test_apply_failure_restores_all_wiki_preimages(tmp_path, failure_stage):
    config, page, plan, updates = _prepared_apply(tmp_path)

    def failpoint(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError(f"injected form failure:{stage}")

    with pytest.raises(RuntimeError, match="all files restored"):
        apply_updates(
            config=config,
            plan=plan,
            updates=updates,
            backup_dir=tmp_path / "backup",
            expected_plan_hash=plan["plan_hash"],
            daemon_check=lambda _database_dir: True,
            projection_committer=lambda **_kwargs: {"ok": True},
            failpoint=failpoint,
        )
    assert page.read_text(encoding="utf-8") == _page()


def test_restart_recovery_restores_source_materialized_generation(tmp_path, monkeypatch):
    import scripts.reconcile_wiki_knowledge_forms as module

    config, page, plan, updates = _prepared_apply(tmp_path)
    backup = tmp_path / "backup"
    original_atomic_write = module._atomic_write

    def interrupt_rollback(path, content):
        if path == page and content == _page():
            raise RuntimeError("simulated process death during rollback")
        return original_atomic_write(path, content)

    monkeypatch.setattr(module, "_atomic_write", interrupt_rollback)

    def failpoint(stage: str) -> None:
        if stage == "before_projection_commit":
            raise RuntimeError("simulated process death")

    with pytest.raises(RuntimeError, match="simulated process death during rollback"):
        apply_updates(
            config=config,
            plan=plan,
            updates=updates,
            backup_dir=backup,
            expected_plan_hash=plan["plan_hash"],
            daemon_check=lambda _database_dir: True,
            projection_committer=lambda **_kwargs: {"ok": True},
            failpoint=failpoint,
        )
    assert "知识形态: 洞察关联" in page.read_text(encoding="utf-8")

    monkeypatch.setattr(module, "_atomic_write", original_atomic_write)
    recovery = recover_incomplete_generation(
        config=config,
        backup_dir=backup,
        daemon_check=lambda _database_dir: True,
    )
    assert recovery["status"] == "recovered"
    assert page.read_text(encoding="utf-8") == _page()


def test_failure_after_projection_commit_rolls_back_projection_and_wiki(
    tmp_path,
    monkeypatch,
):
    import scripts.reconcile_wiki_acl_projection as projection_module

    config, page, plan, updates = _prepared_apply(tmp_path)
    backup = tmp_path / "backup"
    recovered = []

    def projection_committer(**kwargs):
        kwargs["backup_dir"].mkdir(parents=True)
        return {"ok": True, "diagnostics": {"update_count": 1}}

    monkeypatch.setattr(
        projection_module,
        "_recover_wiki_projection_databases_unlocked",
        lambda **kwargs: recovered.append(kwargs)
        or {
            "found": True,
            "status": "recovered_rollback",
        },
    )

    def failpoint(stage: str) -> None:
        if stage == "after_projection_commit":
            raise RuntimeError("fail after projection commit")

    with pytest.raises(RuntimeError, match="all files restored"):
        apply_updates(
            config=config,
            plan=plan,
            updates=updates,
            backup_dir=backup,
            expected_plan_hash=plan["plan_hash"],
            daemon_check=lambda _database_dir: True,
            projection_committer=projection_committer,
            failpoint=failpoint,
        )
    assert recovered
    assert page.read_text(encoding="utf-8") == _page()
