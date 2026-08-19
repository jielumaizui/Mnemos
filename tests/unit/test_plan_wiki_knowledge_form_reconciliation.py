from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3

import pytest

import scripts.plan_wiki_knowledge_form_reconciliation as plan_module
from scripts.plan_wiki_knowledge_form_reconciliation import build_plan


def _page(*, title: str, session_id: str, body: str) -> str:
    return (
        "---\n"
        f"名称: {title}\n"
        "领域: 测试\n"
        "摘要: 历史知识形态恢复测试页面。\n"
        f"来源会话: {session_id}\n"
        "蒸馏时间: '2026-07-23 00:00:00'\n"
        "---\n"
        f"# {title}\n\n{body}\n"
    )


def _checkpoint_db(path, *, session_id: str, title: str, form: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE distill_chunk_results (
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                fragment_json TEXT NOT NULL
            )""")
        conn.execute(
            "INSERT INTO distill_chunk_results VALUES (?, 'completed', ?)",
            (session_id, json.dumps([{"title": title, "form": form}])),
        )


def test_plan_recovers_exact_template_and_matching_checkpoint(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    checkpoint = tmp_path / "chunks.db"
    title = "Decision fixture"
    page = wiki / "decision.md"
    page.write_text(
        _page(
            title=title,
            session_id="session-1",
            body="做同类取舍时，优先看结论、适用场景和不适用边界。",
        ),
        encoding="utf-8",
    )
    _checkpoint_db(
        checkpoint,
        session_id="session-1",
        title=title,
        form="decision",
    )

    report = build_plan(wiki_dir=wiki, checkpoint_db=checkpoint)

    assert report["ok"] is True
    assert report["recoverable_count"] == 1
    assert report["updates"][0]["form"] == "决策记录"
    assert report["recovery_source_counts"] == {
        "template_signature": 1,
        "checkpoint": 1,
        "manual_review": 0,
    }


def test_plan_blocks_conflicting_template_and_checkpoint(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    checkpoint = tmp_path / "chunks.db"
    title = "Conflict fixture"
    (wiki / "conflict.md").write_text(
        _page(
            title=title,
            session_id="session-2",
            body="做同类取舍时，优先看结论、适用场景和不适用边界。",
        ),
        encoding="utf-8",
    )
    _checkpoint_db(
        checkpoint,
        session_id="session-2",
        title=title,
        form="经验法则",
    )

    report = build_plan(wiki_dir=wiki, checkpoint_db=checkpoint)

    assert report["ok"] is False
    assert report["conflict_count"] == 1
    assert report["conflicts"][0]["candidate_forms"] == ["决策记录", "经验法则"]


def test_plan_accepts_hash_bound_manual_review(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "review.md"
    page.write_text(
        _page(title="Review fixture", session_id="session-3", body="Historical body."),
        encoding="utf-8",
    )
    before_hash = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
    review = tmp_path / "review.json"
    review.write_text(
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

    report = build_plan(wiki_dir=wiki, review_manifest=review)

    assert report["ok"] is True
    assert report["updates"][0]["form"] == "洞察关联"
    assert report["recovery_source_counts"]["manual_review"] == 1


def test_plan_rejects_stale_manual_review_hash(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "review.md").write_text(
        _page(title="Review fixture", session_id="session-4", body="Changed body."),
        encoding="utf-8",
    )
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "schema_version": "mnemos.wiki_knowledge_form_review.v1",
                "entries": [
                    {
                        "relative_path": "review.md",
                        "before_sha256": "sha256:" + "0" * 64,
                        "form": "洞察关联",
                        "evidence": "human-review:test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_plan(wiki_dir=wiki, review_manifest=review)

    assert report["ok"] is False
    assert report["unresolved_count"] == 1
    assert report["updates"] == []


@pytest.mark.parametrize(
    ("raw_form", "expected_display"),
    [
        (" 洞察 ", "洞察关联"),
        (" INSIGHT ", "洞察关联"),
        (" ＩＮＳＩＧＨＴ ", "洞察关联"),
        (" Decision-Log ", "决策记录"),
        (" 问题-解决 ", "问题-解决"),
    ],
)
def test_plan_checkpoint_forms_use_canonical_unicode_case_and_alias_corpus(
    tmp_path,
    raw_form,
    expected_display,
):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    checkpoint = tmp_path / "chunks.db"
    title = f"Canonical fixture {expected_display}"
    (wiki / "canonical.md").write_text(
        _page(title=title, session_id="canonical-session", body="No template signature."),
        encoding="utf-8",
    )
    _checkpoint_db(
        checkpoint,
        session_id="canonical-session",
        title=title,
        form=raw_form,
    )

    report = build_plan(wiki_dir=wiki, checkpoint_db=checkpoint)

    assert report["ok"] is True
    assert report["updates"][0]["form"] == expected_display


def test_plan_existing_chinese_insight_alias_is_already_covered(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = _page(
        title="Existing insight alias",
        session_id="canonical-existing",
        body="Historical body.",
    ).replace("蒸馏时间:", "知识形态: 洞察\n蒸馏时间:")
    (wiki / "existing.md").write_text(page, encoding="utf-8")

    report = build_plan(wiki_dir=wiki)

    assert report["ok"] is True
    assert report["eligible_page_count"] == 1
    assert report["already_covered_count"] == 1
    assert report["updates"] == []


def test_plan_module_does_not_own_a_second_form_alias_vocabulary():
    source = inspect.getsource(plan_module)

    assert "FORM_ALIASES =" not in source
