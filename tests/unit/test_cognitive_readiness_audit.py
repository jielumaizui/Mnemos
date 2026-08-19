import argparse
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def _create_wiki_metrics_db(path, wiki_dir):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE page_metrics (
                wiki_path TEXT PRIMARY KEY,
                page_role TEXT DEFAULT 'knowledge',
                source_count INTEGER DEFAULT 0,
                source_refs TEXT DEFAULT '[]'
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO page_metrics (wiki_path, page_role, source_count, source_refs)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("with-source.md", "knowledge", 2, '["raw-1"]'),
                (
                    str(wiki_dir / "nested" / "absolute-source.md"),
                    "knowledge",
                    1,
                    '["raw-3"]',
                ),
                ("missing-source.md", "knowledge", 0, "[]"),
                ("03-Tech/placeholder.md", "generated_placeholder", 0, "[]"),
                (str(wiki_dir / "stale" / "old-page.md"), "knowledge", 0, "[]"),
            ],
        )


def _create_raw_events_db(path):
    """Create real current Raw revisions rather than source_id-shaped fixtures."""
    from core.sync_framework.raw_event_store import RawEventStore

    store = RawEventStore(db_path=path)
    try:
        active_revision = store.upsert_turn(
            source_agent="codex",
            session_id="readiness-active",
            turn_number=1,
            user_content="active user evidence",
            assistant_content="active assistant evidence",
            tool_calls=[{"name": "run"}],
            source_files=["a.py"],
            completeness={"visible_text": "full"},
        )
        eligible_delete_revision = store.upsert_turn(
            source_agent="codex",
            session_id="readiness-retained",
            turn_number=2,
            user_content="partial user evidence",
            assistant_content="partial assistant evidence",
            tool_results=[{"ok": True}],
            attachments=[{"id": "shot"}],
            completeness={"truncated": True},
        )
        active_event_id = store.get_turn(active_revision)["logical_event_id"]
        active_content_hash = store.get_turn(active_revision)["content_hash"]
        eligible_delete_event_id = store.get_turn(eligible_delete_revision)["logical_event_id"]
        conn = store._pool.get_conn()  # noqa: SLF001 - test-only retention setup
        conn.execute(
            "UPDATE raw_metrics SET retention_state='eligible_delete' WHERE event_id=?",
            (eligible_delete_event_id,),
        )
        conn.commit()
        return {
            "active_revision": active_revision,
            "active_event_id": active_event_id,
            "active_content_hash": active_content_hash,
            "eligible_delete_revision": eligible_delete_revision,
            "eligible_delete_event_id": eligible_delete_event_id,
        }
    finally:
        store.close()


def _record_observation_edge(raw_db, revision_id, observation_id):
    from core.sync_framework.raw_event_store import RawEventStore, canonical_observation_text

    store = RawEventStore(db_path=raw_db)
    try:
        turn = store.get_turn(revision_id)
        assert turn is not None
        visible_text = canonical_observation_text(turn)
        store.record_provenance_edge(
            source_revision_id=revision_id,
            span_start=0,
            span_end=len(visible_text),
            consumer_type="observation",
            consumer_id=observation_id,
        )
    finally:
        store.close()


def _frozen_consolidation_report(revision_id, *, content_hash="source-hash"):
    """Build the immutable candidate snapshot required by COG-031/024."""
    return json.dumps(
        {
            "coverage": {
                "candidate_dispositions": [
                    {
                        "source_event_id": "event-1",
                        "source_revision_id": revision_id,
                        "source_content_hash": content_hash,
                        "exact_source_ref": f"raw-revision:{revision_id}",
                    }
                ]
            }
        },
        sort_keys=True,
    )


def _create_feedback_dbs(database_dir):
    with sqlite3.connect(database_dir / "dialog_reminder.db") as conn:
        conn.execute(
            """
            CREATE TABLE dialog_reminders (
                reminder_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                resolved_choice TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO dialog_reminders (reminder_id, status, resolved_choice) VALUES (?, ?, ?)",
            [("r1", "pending", ""), ("r2", "resolved", "done")],
        )

    with sqlite3.connect(database_dir / "mnemos.db") as conn:
        conn.execute(
            """
            CREATE TABLE search_sessions (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                clicked_path TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO search_sessions (session_id, clicked_path) VALUES (?, ?)",
            [("s1", ""), ("s2", "03-Tech/x.md")],
        )

    with sqlite3.connect(database_dir / "recap_tasks.db") as conn:
        conn.execute(
            """
            CREATE TABLE recap_tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO recap_tasks (task_id, status) VALUES (?, ?)",
            [("t1", "pending"), ("t2", "resolved")],
        )


def _create_learning_signal_dbs(database_dir, *, active_revision):
    with sqlite3.connect(database_dir / "observations.db") as conn:
        conn.execute(
            """
            CREATE TABLE observations (
                id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_id TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO observations (id, source_type, source_id, created_at, updated_at)
            VALUES ('obs-1', 'raw', ?, ?, ?)
            """,
            (active_revision, now, now),
        )

    _record_observation_edge(database_dir / "raw_events.db", active_revision, "obs-1")

    with sqlite3.connect(database_dir / "reflections.db") as conn:
        conn.execute("CREATE TABLE reflection_records (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO reflection_records (id) VALUES ('ref-1')")


@pytest.fixture
def fake_config(tmp_path):
    database_dir = tmp_path / "runtime-empty"
    database_dir.mkdir()
    wiki_dir = tmp_path / "mnemos-vault"
    wiki_dir.mkdir()
    (wiki_dir / "nested").mkdir()
    (wiki_dir / "with-source.md").write_text(
        "---\nsource_event_id: raw-1\n---\n\nbody",
        encoding="utf-8",
    )
    (wiki_dir / "nested" / "absolute-source.md").write_text(
        "---\nsource_event_id: raw-3\n---\n\nbody",
        encoding="utf-8",
    )
    (wiki_dir / "missing-source.md").write_text("---\n---\n\nbody", encoding="utf-8")
    (wiki_dir / "03-Tech").mkdir()
    (wiki_dir / "03-Tech" / "placeholder.md").write_text(
        "---\n名称: 占位页\n---\n\n"
        "# placeholder\n\n"
        "该页面为自动创建的占位/消歧页，用于修复悬空链接。需要后续补充实质内容。\n",
        encoding="utf-8",
    )
    (wiki_dir / "L2.4-KG" / "Entities").mkdir(parents=True)
    (wiki_dir / "L2.4-KG" / "Entities" / "agent.md").write_text(
        "---\ntitle: agent\n---\n\n# agent\n",
        encoding="utf-8",
    )

    _create_wiki_metrics_db(database_dir / "wiki_metrics.db", wiki_dir)
    raw_refs = _create_raw_events_db(database_dir / "raw_events.db")
    _create_feedback_dbs(database_dir)
    _create_learning_signal_dbs(database_dir, active_revision=raw_refs["active_revision"])

    return SimpleNamespace(
        database_dir=database_dir,
        wiki_dir=wiki_dir,
        obsidian_vault_path=tmp_path / "raw-vault",
    )


def test_cognitive_readiness_report_counts_evidence_and_feedback_gaps(fake_config):
    from core.ops.cognitive_readiness import build_cognitive_readiness_report

    report = build_cognitive_readiness_report(fake_config)

    assert report["schema_version"] == "mnemos.cognitive_readiness.v2"
    assert report["ok"] is False
    assert report["metrics"]["page_metrics"]["total"] == 3
    assert report["metrics"]["page_metrics"]["wiki_page_total"] == 5
    assert report["metrics"]["page_metrics"]["stale_metric_rows"] == 1
    assert report["metrics"]["page_metrics"]["source_exempt_total"] == 2
    assert report["metrics"]["page_metrics"]["source_exempt_reasons"] == {
        "generated_placeholder": 1,
        "derived_artifact:L2.4-KG": 1,
    }
    assert report["metrics"]["page_metrics"]["source_count_zero"] == 1
    assert report["metrics"]["page_metrics"]["source_refs_nonempty"] == 2
    assert report["metrics"]["page_metrics"]["source_count_positive_empty_refs"] == 0
    assert report["metrics"]["wiki_pages"]["with_source_refs"] == 2
    assert report["metrics"]["wiki_pages"]["source_required_total"] == 3
    assert report["metrics"]["wiki_pages"]["source_exempt_total"] == 2
    assert report["metrics"]["wiki_pages"]["missing_source_refs"] == 1
    assert report["metrics"]["raw_turns"]["total"] == 2
    assert report["metrics"]["raw_turns"]["status_counts"] == {"complete": 1, "partial": 1}
    assert report["metrics"]["raw_turns"]["with_tool_calls"] == 1
    assert report["metrics"]["raw_retention"]["status_counts"] == {
        "active": 1,
        "eligible_delete": 1,
    }
    assert report["metrics"]["dialog_reminders"]["status_counts"] == {
        "pending": 1,
        "resolved": 1,
    }
    assert report["metrics"]["search_sessions"]["clicked"] == 1
    learning_signal = report["metrics"]["learning_signal"]
    assert learning_signal["schema_version"] == "mnemos.learning_signal.v2"
    assert learning_signal["raw_signal_count"] == 2
    assert learning_signal["observation_count"] == 1
    assert learning_signal["lineage_coverage"]["raw_to_observation"] == {
        "denominator": 1,
        "covered": 1,
        "uncovered": 0,
        "coverage_ratio": 1.0,
        "lineage_refs": learning_signal["lineage_coverage"]["raw_to_observation"]["lineage_refs"],
        "lineage_ref_count": 1,
        "lineage_refs_truncated": False,
        "freshness_at": learning_signal["lineage_coverage"]["raw_to_observation"]["freshness_at"],
        "freshness_state": "fresh",
        "cold_start_state": "observed",
    }
    assert learning_signal["lineage_coverage"]["raw_to_observation"]["lineage_refs"][0].startswith(
        "rawrev-"
    )
    assert learning_signal["reflection_count"] == 1
    assert learning_signal["policy_patch_count"] == 0
    assert learning_signal["policy_patch_gap"] == 1
    assert learning_signal["consolidation_run_gap"] == 1
    assert learning_signal["observation_output_gap"] == 0
    assert learning_signal["observation_lineage_gap"] == 0
    assert learning_signal["policy_patch_status"] == "candidate_activity_without_policy_patch"
    assert learning_signal["consolidation_status"] == "not_initialized"
    assert report["metrics"]["delivery_events"]["exists"] is False
    assert report["readiness"]["source"]["status"] == "degraded"
    assert report["readiness"]["evidence"]["status"] == "degraded"
    assert report["readiness"]["consumer"]["status"] == "blocked"
    assert report["readiness"]["behavior"]["status"] == "degraded"
    assert report["budget"]["ok"] is False
    assert report["scorecard"]["dimension"] == "cognitive_assets"
    assert report["scorecard"]["score_name"] == "cognitive_maturity_readiness"
    assert report["scorecard"]["runtime_metrics"]["learning_signal.policy_patch_gap"] == 1
    assert report["readiness"]["consumer"]["metrics"]["learning_signal"]["schema_version"] == (
        "mnemos.learning_signal.v2"
    )
    assert any(f["code"] == "delivery_events_missing" for f in report["findings"])
    assert any(f["code"] == "learning_policy_patch_gap" for f in report["findings"])


def test_cognitive_readiness_strict_budget_and_action_ledger(fake_config):
    from core.ops.cognitive_readiness import (
        build_cognitive_readiness_report,
        record_cognitive_readiness_gaps,
    )
    from core.system_contracts import ActionLedger

    report = build_cognitive_readiness_report(fake_config, strict=True)

    assert report["ok"] is False
    failure_codes = {item["code"] for item in report["budget"]["failures"]}
    assert {
        "wiki_source_ref_gap",
        "dialog_reminder_backlog",
        "learning_policy_patch_gap",
        "learning_consolidation_run_gap",
    } <= failure_codes

    action_id = record_cognitive_readiness_gaps(report, fake_config)

    assert action_id
    rows = ActionLedger(fake_config.database_dir / "action_ledger.db").recent()
    assert rows[0]["action_type"] == "cognitive_readiness_gap"
    assert rows[0]["status"] == "needs_user"
    assert rows[0]["verification"]["budget_ok"] is False
    assert report["action_ledger"]["action_id"] == action_id


def test_health_surfaces_versioned_lineage_and_freshness(fake_config):
    from core.ops.health_check import _check_cognitive_learning, _check_cognitive_readiness

    learning = _check_cognitive_learning(fake_config)
    readiness = _check_cognitive_readiness(fake_config)

    assert learning["schema_version"] == "mnemos.learning_signal.v2"
    assert learning["lineage_coverage"]["raw_to_observation"]["denominator"] == 1
    assert learning["gaps"]["observation_lineage_gap"] == 0
    assert learning["cold_start_state"] == "blocked"
    assert readiness["schema_version"] == "mnemos.cognitive_readiness.v2"
    assert readiness["status"] == "degraded"
    assert readiness["lineage_coverage"]["raw_to_observation"]["covered"] == 1
    assert "required_evidence_unavailable" in readiness["blocking_findings"]


def test_cognitive_readiness_report_blocks_when_required_databases_are_missing(tmp_path):
    from core.ops.cognitive_readiness import build_cognitive_readiness_report

    database_dir = tmp_path / ".mnemos"
    database_dir.mkdir(exist_ok=True)
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    config = SimpleNamespace(database_dir=database_dir, wiki_dir=wiki_dir)

    report = build_cognitive_readiness_report(config)

    assert report["ok"] is False
    assert report["metrics"]["raw_turns"]["exists"] is False
    assert report["metrics"]["page_metrics"]["exists"] is False
    assert report["readiness"]["source"]["status"] == "blocked"
    assert report["scorecard"]["score"] < 100
    assert report["scorecard"]["blocking_findings"]
    assert any(f["code"] == "raw_events_missing" for f in report["findings"])


def test_initialized_empty_required_schemas_cannot_score_100(tmp_path):
    from core.ops.cognitive_readiness import build_cognitive_readiness_report

    database_dir = tmp_path / ".mnemos"
    database_dir.mkdir()
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    with sqlite3.connect(database_dir / "raw_events.db") as conn:
        conn.execute("CREATE TABLE raw_turns (event_id TEXT PRIMARY KEY, updated_at TEXT)")
        conn.execute("CREATE TABLE raw_metrics (event_id TEXT PRIMARY KEY, retention_state TEXT)")
    with sqlite3.connect(database_dir / "wiki_metrics.db") as conn:
        conn.execute(
            """
            CREATE TABLE page_metrics (
                wiki_path TEXT PRIMARY KEY,
                page_role TEXT,
                source_count INTEGER,
                source_refs TEXT
            )
            """
        )
    with sqlite3.connect(database_dir / "delivery_events.db") as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                event_id TEXT PRIMARY KEY, created_at TEXT, feedback TEXT,
                feedback_at TEXT, outcome_id TEXT, decision TEXT, delivered_level TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE cognitive_outcomes (
                outcome_id TEXT PRIMARY KEY, created_at TEXT, delivery_event_id TEXT
            )
            """
        )
    with sqlite3.connect(database_dir / "observations.db") as conn:
        conn.execute(
            """
            CREATE TABLE observations (
                id TEXT PRIMARY KEY, source_type TEXT, source_id TEXT,
                created_at TEXT, updated_at TEXT
            )
            """
        )
    with sqlite3.connect(database_dir / "reflections.db") as conn:
        conn.execute("CREATE TABLE reflection_records (id TEXT PRIMARY KEY)")
    with sqlite3.connect(database_dir / "policy_patches.db") as conn:
        conn.execute(
            """
            CREATE TABLE policy_patches (
                patch_id TEXT PRIMARY KEY, source_type TEXT, source_id TEXT, created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE policy_patch_feedback (
                feedback_id TEXT PRIMARY KEY, patch_id TEXT, outcome TEXT,
                evidence_json TEXT, source_event_id TEXT, created_at TEXT
            )
            """
        )
    with sqlite3.connect(database_dir / "cognitive_consolidation.db") as conn:
        conn.execute(
            """
            CREATE TABLE consolidation_runs (
                run_id TEXT PRIMARY KEY, created_at TEXT, applied INTEGER,
                raw_candidate_count INTEGER, report_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE consolidation_coverage (
                run_id TEXT, source_event_id TEXT, created_at TEXT,
                PRIMARY KEY (run_id, source_event_id)
            )
            """
        )
    with sqlite3.connect(database_dir / "mnemos.db") as conn:
        conn.execute(
            """
            CREATE TABLE search_sessions (
                id INTEGER PRIMARY KEY, clicked_path TEXT, opened_path TEXT,
                ignored_at TEXT, outcome_status TEXT, outcome_at TEXT
            )
            """
        )
    with sqlite3.connect(database_dir / "dialog_reminder.db") as conn:
        conn.execute("CREATE TABLE dialog_reminders (reminder_id TEXT PRIMARY KEY, status TEXT)")
    with sqlite3.connect(database_dir / "recap_tasks.db") as conn:
        conn.execute("CREATE TABLE recap_tasks (task_id TEXT PRIMARY KEY, status TEXT)")
    with sqlite3.connect(database_dir / "knowledge_graph.db") as conn:
        for table in ("entities", "relations", "relation_evidence", "document_wiki_link"):
            conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
    with sqlite3.connect(database_dir / "cognitive_graph.db") as conn:
        for table in ("canonical_nodes", "cognitive_relations"):
            conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
    with sqlite3.connect(database_dir / "evidence_graph.db") as conn:
        for table in ("evidence_nodes", "evidence_edges"):
            conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")

    report = build_cognitive_readiness_report(
        SimpleNamespace(database_dir=database_dir, wiki_dir=wiki_dir), strict=True
    )

    assert report["ok"] is False
    assert report["scorecard"]["score"] < 100
    assert report["metrics"]["learning_signal"]["cold_start_state"] == "blocked"
    assert any(item["code"] == "required_evidence_empty" for item in report["findings"])
    assert any(item["code"] == "learning_lineage_unobserved" for item in report["findings"])


def test_fully_linked_fresh_evidence_scores_100_and_budget_cli_passes(tmp_path, capsys):
    from core.ops.cognitive_readiness import build_cognitive_readiness_report
    from scripts.audit_cognitive_readiness import main

    database_dir = tmp_path / "db"
    database_dir.mkdir()
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    raw_vault = tmp_path / "raw"
    raw_vault.mkdir()
    now = datetime.now(timezone.utc).isoformat()
    (wiki_dir / "knowledge.md").write_text(
        "---\nsource_event_id: raw-1\n---\n\n# Knowledge\n",
        encoding="utf-8",
    )
    with sqlite3.connect(database_dir / "wiki_metrics.db") as conn:
        conn.execute(
            """
            CREATE TABLE page_metrics (
                wiki_path TEXT PRIMARY KEY, page_role TEXT,
                source_count INTEGER, source_refs TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO page_metrics VALUES ('knowledge.md', 'knowledge', 1, '[\"raw-1\"]')"
        )
    raw_refs = _create_raw_events_db(database_dir / "raw_events.db")
    with sqlite3.connect(database_dir / "mnemos.db") as conn:
        conn.execute(
            """
            CREATE TABLE search_sessions (
                id INTEGER PRIMARY KEY, clicked_path TEXT, opened_path TEXT,
                ignored_at TEXT, outcome_status TEXT, outcome_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO search_sessions VALUES (1, 'knowledge.md', 'knowledge.md', '', 'click', ?)",
            (now,),
        )
    with sqlite3.connect(database_dir / "dialog_reminder.db") as conn:
        conn.execute(
            """
            CREATE TABLE dialog_reminders (
                reminder_id TEXT PRIMARY KEY, status TEXT, resolved_choice TEXT
            )
            """
        )
        conn.execute("INSERT INTO dialog_reminders VALUES ('rem-1', 'resolved', 'done')")
    with sqlite3.connect(database_dir / "recap_tasks.db") as conn:
        conn.execute("CREATE TABLE recap_tasks (task_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO recap_tasks VALUES ('recap-1', 'resolved')")
    with sqlite3.connect(database_dir / "delivery_events.db") as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                event_id TEXT PRIMARY KEY, created_at TEXT, feedback TEXT,
                feedback_at TEXT, outcome_id TEXT, decision TEXT, delivered_level TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO delivery_events VALUES ('delivery-1', ?, '', '', 'outcome-1', 'deliver', 'hint')",
            (now,),
        )
        conn.execute(
            """
            CREATE TABLE cognitive_outcomes (
                outcome_id TEXT PRIMARY KEY, created_at TEXT, delivery_event_id TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO cognitive_outcomes VALUES ('outcome-1', ?, 'delivery-1')",
            (now,),
        )
    with sqlite3.connect(database_dir / "observations.db") as conn:
        conn.execute(
            """
            CREATE TABLE observations (
                id TEXT PRIMARY KEY, source_type TEXT, source_id TEXT,
                created_at TEXT, updated_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO observations VALUES ('obs-1', 'raw', ?, ?, ?)",
            (raw_refs["active_revision"], now, now),
        )
    _record_observation_edge(database_dir / "raw_events.db", raw_refs["active_revision"], "obs-1")
    with sqlite3.connect(database_dir / "reflections.db") as conn:
        conn.execute("CREATE TABLE reflection_records (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO reflection_records VALUES ('reflection-1')")
    with sqlite3.connect(database_dir / "policy_patches.db") as conn:
        conn.execute(
            """
            CREATE TABLE policy_patches (
                patch_id TEXT PRIMARY KEY, source_type TEXT, source_id TEXT, created_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO policy_patches VALUES ('patch-1', 'reflection', 'reflection-1', ?)",
            (now,),
        )
        conn.execute(
            """
            CREATE TABLE policy_patch_feedback (
                feedback_id TEXT PRIMARY KEY, patch_id TEXT, outcome TEXT,
                evidence_json TEXT, source_event_id TEXT, created_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO policy_patch_feedback VALUES ('feedback-1', 'patch-1', 'accepted', '{}', 'event-1', ?)",
            (now,),
        )
    with sqlite3.connect(database_dir / "cognitive_consolidation.db") as conn:
        conn.execute(
            """
            CREATE TABLE consolidation_runs (
                run_id TEXT PRIMARY KEY, created_at TEXT, applied INTEGER,
                raw_candidate_count INTEGER, report_json TEXT
            )
            """
        )
        report_json = _frozen_consolidation_report(raw_refs["active_revision"])
        conn.execute(
            "INSERT INTO consolidation_runs VALUES ('run-1', ?, 1, 1, ?)",
            (now, report_json),
        )
        conn.execute(
            """
            CREATE TABLE consolidation_coverage_receipts (
                run_id TEXT, source_event_id TEXT, source_revision_id TEXT,
                source_content_hash TEXT, exact_source_ref TEXT, covered_by TEXT,
                method_content_hash TEXT, mutation_id TEXT, created_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO consolidation_coverage_receipts VALUES
            ('run-1', 'event-1', ?, 'source-hash', ?, 'method.md', 'method-hash', 'mutation-1', ?)
            """,
            (raw_refs["active_revision"], f"raw-revision:{raw_refs['active_revision']}", now),
        )
    with sqlite3.connect(database_dir / "knowledge_graph.db") as conn:
        for table in ("entities", "relations", "relation_evidence", "document_wiki_link"):
            conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
    with sqlite3.connect(database_dir / "cognitive_graph.db") as conn:
        for table in ("canonical_nodes", "cognitive_relations"):
            conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
    with sqlite3.connect(database_dir / "evidence_graph.db") as conn:
        for table in ("evidence_nodes", "evidence_edges"):
            conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")

    config = SimpleNamespace(
        database_dir=database_dir,
        wiki_dir=wiki_dir,
        obsidian_vault_path=raw_vault,
    )
    report = build_cognitive_readiness_report(config, strict=True, enforce_budget=True)

    assert report["ok"] is True
    assert report["budget"]["failure_count"] == 0
    assert report["scorecard"]["score"] == 100
    assert report["scorecard"]["blocking_findings"] == []
    for metric in report["metrics"]["learning_signal"]["lineage_coverage"].values():
        assert metric["coverage_ratio"] == 1.0
        assert metric["freshness_state"] == "fresh"

    rc = main(
        [
            "--database-dir",
            str(database_dir),
            "--wiki-dir",
            str(wiki_dir),
            "--raw-vault-dir",
            str(raw_vault),
            "--json",
            "--budget",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["scorecard"]["score"] == 100


def test_delivery_feedback_requires_explicit_linked_effect(tmp_path):
    from core.ops.cognitive_readiness import _delivery_outcome_metric

    db_path = tmp_path / "delivery_events.db"
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                event_id TEXT PRIMARY KEY, created_at TEXT, feedback TEXT,
                feedback_at TEXT, outcome_id TEXT, decision TEXT, delivered_level TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE cognitive_outcomes (
                outcome_id TEXT PRIMARY KEY, created_at TEXT, delivery_event_id TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO delivery_events VALUES (?, ?, ?, '', ?, 'deliver', 'hint')",
            [
                ("d1", now, "", ""),
                ("d2", now, "useful", ""),
                ("d3", now, "", "out-3"),
            ],
        )
        conn.executemany(
            "INSERT INTO cognitive_outcomes VALUES (?, ?, ?)",
            [("out-3", now, "d3"), ("orphan", now, "missing-delivery")],
        )

    metric = _delivery_outcome_metric(db_path)

    assert metric["denominator"] == 3
    assert metric["explicit_feedback_count"] == 1
    assert metric["linked_outcome_count"] == 1
    assert metric["covered"] == 2
    assert metric["coverage_ratio"] == pytest.approx(2 / 3, abs=0.0001)
    assert metric["unlinked_outcome_count"] == 1
    assert metric["lineage_refs"] == ["d2", "d3"]


def test_delivery_feedback_denominator_excludes_suppressed_and_silent_decisions(tmp_path):
    from core.ops.cognitive_readiness import _delivery_outcome_metric

    db_path = tmp_path / "delivery_events.db"
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                event_id TEXT PRIMARY KEY, created_at TEXT, feedback TEXT,
                feedback_at TEXT, outcome_id TEXT, decision TEXT, delivered_level TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE cognitive_outcomes (outcome_id TEXT PRIMARY KEY, created_at TEXT, delivery_event_id TEXT)"
        )
        conn.executemany(
            "INSERT INTO delivery_events VALUES (?, ?, ?, '', '', ?, ?)",
            [
                ("visible", now, "", "deliver", "hint"),
                ("silent", now, "", "deliver", "silent"),
                ("suppressed", now, "", "suppress", "silent"),
            ],
        )

    metric = _delivery_outcome_metric(db_path)

    assert metric["denominator"] == 1
    assert metric["uncovered"] == 1


def test_delivery_feedback_requires_presentation_receipt_and_exact_terminal_links(tmp_path):
    from core.ops.cognitive_readiness import _delivery_outcome_metric

    database_dir = tmp_path / "db"
    database_dir.mkdir()
    delivery_db = database_dir / "delivery_events.db"
    state_db = database_dir / "producer_consumer_ledger.db"
    reminders_db = database_dir / "dialog_reminder.db"
    now = datetime.now(timezone.utc).isoformat()
    principal = {"principal_id": "operator-1", "agent": "cli", "capability_id": "push"}
    scope = {"scope_type": "project", "scope_id": "mnemos", "project": "mnemos", "session_id": ""}

    with sqlite3.connect(delivery_db) as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                event_id TEXT PRIMARY KEY, created_at TEXT, decision TEXT,
                delivered_level TEXT, metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE delivery_presentation_receipts (
                event_id TEXT PRIMARY KEY, recorded_at TEXT, host_agent TEXT,
                rendered_content_hash TEXT, delivery_event_hash TEXT, receipt_hash TEXT
            )
            """
        )
        metadata = json.dumps({"delivery_principal": principal})
        conn.executemany(
            "INSERT INTO delivery_events VALUES (?, ?, 'deliver', 'hint', ?)",
            [(event_id, now, metadata) for event_id in ("shown", "routed", "unshown", "cross")],
        )
        conn.executemany(
            "INSERT INTO delivery_presentation_receipts VALUES (?, ?, 'cli', ?, ?, ?)",
            [
                ("shown", now, "sha256:" + "a" * 64, "effect-shown", "receipt-shown"),
                ("cross", now, "sha256:" + "b" * 64, "effect-cross", "receipt-cross"),
            ],
        )

    with sqlite3.connect(state_db) as conn:
        conn.execute(
            """
            CREATE TABLE cognitive_state_revisions (
                revision_id TEXT PRIMARY KEY, object_type TEXT, created_at TEXT, payload_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE cognitive_state_heads (revision_id TEXT PRIMARY KEY)"
        )
        records = [
            (
                "prediction-shown",
                "prediction_record",
                {"delivery_ref": {"event_id": "shown"}, "access_control": {"scope": scope}},
            ),
            (
                "prediction-cross",
                "prediction_record",
                {"delivery_ref": {"event_id": "cross"}, "access_control": {"scope": scope}},
            ),
            (
                "reaction-shown",
                "user_reaction_event",
                {
                    "delivery_ref": {"event_id": "shown"},
                    "display_ref": {"display_id": "receipt-shown", "content_hash": "sha256:" + "a" * 64},
                },
            ),
            (
                "outcome-unshown",
                "outcome_measurement",
                {"delivery_ref": {"event_id": "unshown"}},
            ),
            (
                "outcome-cross",
                "outcome_measurement",
                {
                    "delivery_ref": {"event_id": "cross"},
                    "presentation_ref": {
                        "receipt_hash": "receipt-cross",
                        "rendered_content_hash": "sha256:" + "b" * 64,
                        "delivery_event_hash": "effect-cross",
                    },
                    "access_control": {
                        "owner": {"principal_id": "operator-1", "agent": "cli"},
                        "scope": {**scope, "scope_id": "other"},
                    },
                },
            ),
        ]
        conn.executemany(
            "INSERT INTO cognitive_state_revisions VALUES (?, ?, ?, ?)",
            [(revision_id, object_type, now, json.dumps(payload)) for revision_id, object_type, payload in records],
        )
        conn.executemany(
            "INSERT INTO cognitive_state_heads VALUES (?)",
            [(revision_id,) for revision_id, _object_type, _payload in records],
        )

    with sqlite3.connect(reminders_db) as conn:
        conn.execute(
            """
            CREATE TABLE dialog_reminders (
                status TEXT, delivery_event_id TEXT, resolved_choice TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO dialog_reminders VALUES ('expired', 'cross', 'presentation_timeout')"
        )

    metric = _delivery_outcome_metric(delivery_db)

    assert metric["denominator"] == 2
    assert metric["covered"] == 2
    assert metric["routed_without_presentation_ack"] == 2
    assert metric["outcome_for_unshown_event"] == 1
    assert metric["cross_scope_outcome_link"] == 1
    assert metric["typed_timeout_count"] == 1
    assert metric["lineage_refs"] == ["cross", "shown"]


def test_lineage_coverage_rejects_global_counts_and_dry_run(tmp_path):
    from core.ops.cognitive_readiness import build_cognitive_readiness_report

    database_dir = tmp_path / "db"
    database_dir.mkdir()
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    raw_refs = _create_raw_events_db(database_dir / "raw_events.db")
    with sqlite3.connect(database_dir / "raw_events.db") as conn:
        conn.execute(
            "UPDATE raw_metrics SET retention_state='active' WHERE event_id=?",
            (raw_refs["eligible_delete_event_id"],),
        )
    _create_wiki_metrics_db(database_dir / "wiki_metrics.db", wiki_dir)
    for name in ("with-source.md", "missing-source.md"):
        (wiki_dir / name).write_text("---\nsource_event_id: raw-1\n---\n", encoding="utf-8")
    (wiki_dir / "nested").mkdir()
    (wiki_dir / "nested" / "absolute-source.md").write_text(
        "---\nsource_event_id: raw-1\n---\n", encoding="utf-8"
    )
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(database_dir / "observations.db") as conn:
        conn.execute(
            """
            CREATE TABLE observations (
                id TEXT PRIMARY KEY, source_type TEXT, source_id TEXT,
                created_at TEXT, updated_at TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO observations VALUES (?, 'raw', ?, ?, ?)",
            [
                ("o1", raw_refs["active_revision"], now, now),
                ("o-unrelated", "other", now, now),
            ],
        )
    _record_observation_edge(database_dir / "raw_events.db", raw_refs["active_revision"], "o1")
    with sqlite3.connect(database_dir / "delivery_events.db") as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                event_id TEXT PRIMARY KEY, created_at TEXT, feedback TEXT,
                feedback_at TEXT, outcome_id TEXT, decision TEXT, delivered_level TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE cognitive_outcomes (outcome_id TEXT PRIMARY KEY, created_at TEXT, delivery_event_id TEXT)"
        )
    with sqlite3.connect(database_dir / "cognitive_consolidation.db") as conn:
        conn.execute(
            """
            CREATE TABLE consolidation_runs (
                run_id TEXT PRIMARY KEY, created_at TEXT, applied INTEGER,
                raw_candidate_count INTEGER, report_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE consolidation_coverage (run_id TEXT, source_event_id TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO consolidation_runs VALUES ('dry', ?, 0, 2, '{}')",
            (now,),
        )

    report = build_cognitive_readiness_report(
        SimpleNamespace(database_dir=database_dir, wiki_dir=wiki_dir), strict=True
    )
    lineage = report["metrics"]["learning_signal"]["lineage_coverage"]

    assert lineage["raw_to_observation"]["denominator"] == 2
    assert lineage["raw_to_observation"]["covered"] == 1
    assert lineage["raw_to_observation"]["uncovered"] == 1
    assert lineage["consolidation_candidate_to_applied"]["denominator"] == 0
    assert lineage["consolidation_candidate_to_applied"]["covered"] == 0
    assert lineage["consolidation_candidate_to_applied"]["coverage_ratio"] == 0.0
    assert lineage["consolidation_candidate_to_applied"]["cold_start_state"] == "blocked"
    assert report["metrics"]["learning_signal"]["consolidation_coverage_gap"] == 0
    assert report["metrics"]["learning_signal"]["consolidation_run_gap"] == 1
    assert report["metrics"]["learning_signal"]["consolidation_status"] == "dry_run_only"
    assert report["ok"] is False


def test_policy_driver_coverage_requires_exact_patch_or_no_patch_lineage(tmp_path):
    from core.ops.cognitive_readiness_lineage import policy_driver_lineage_metric

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(tmp_path / "reflections.db") as conn:
        conn.execute("CREATE TABLE reflection_records (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO reflection_records VALUES (?)", [("r1",), ("r2",), ("r3",)])
    with sqlite3.connect(tmp_path / "recap_tasks.db") as conn:
        conn.execute(
            """
            CREATE TABLE recap_consumption_outcomes (
                recap_id TEXT, consumer TEXT, outcome TEXT, created_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO recap_consumption_outcomes VALUES ('recap-1', 'policy_patch', 'skipped', ?)",
            (now,),
        )
    policy_db = tmp_path / "policy_patches.db"
    with sqlite3.connect(policy_db) as conn:
        conn.execute(
            """
            CREATE TABLE policy_patches (
                patch_id TEXT PRIMARY KEY, source_type TEXT, source_id TEXT, created_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO policy_patches VALUES ('p1', 'reflection', 'r1', ?)",
            (now,),
        )
        conn.execute(
            """
            CREATE TABLE policy_patch_feedback (
                feedback_id TEXT PRIMARY KEY, patch_id TEXT, outcome TEXT,
                evidence_json TEXT, source_event_id TEXT, created_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO policy_patch_feedback
            VALUES ('f1', 'reflection-no-patch-r2', 'no_patch',
                    '{"record_id":"r2"}', '', ?)
            """,
            (now,),
        )

    metric = policy_driver_lineage_metric(
        tmp_path,
        policy_db,
        freshness_window_seconds=30 * 86400,
        now=datetime.now(timezone.utc),
    )

    assert metric["denominator"] == 4
    assert metric["covered"] == 2
    assert metric["uncovered"] == 2
    assert metric["lineage_refs"] == [
        "reflection:r1",
        "reflection:r2",
    ]


def test_legacy_consolidation_coverage_cannot_prove_readiness(tmp_path):
    from core.ops.cognitive_readiness_lineage import consolidation_lineage_metric

    db_path = tmp_path / "cognitive_consolidation.db"
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE consolidation_runs (
                run_id TEXT PRIMARY KEY, created_at TEXT, applied INTEGER,
                raw_candidate_count INTEGER
            )
            """
        )
        conn.executemany(
            "INSERT INTO consolidation_runs VALUES (?, ?, ?, ?)",
            [("applied", now, 1, 2), ("dry", now, 0, 3)],
        )
        conn.execute(
            "CREATE TABLE consolidation_coverage (run_id TEXT, source_event_id TEXT, created_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO consolidation_coverage VALUES (?, ?, ?)",
            [
                ("applied", "raw-1", now),
                ("applied", "raw-2", now),
                ("dry", "raw-3", now),
            ],
        )

    metric = consolidation_lineage_metric(
        db_path,
        freshness_window_seconds=30 * 86400,
        now=datetime.now(timezone.utc),
    )

    assert metric["denominator"] == 0
    assert metric["covered"] == 0
    assert metric["cold_start_state"] == "blocked"


def test_consolidation_receipts_freeze_unique_candidates_and_reject_duplicates(tmp_path):
    from core.ops.cognitive_readiness_lineage import consolidation_lineage_metric

    db_path = tmp_path / "cognitive_consolidation.db"
    now = datetime.now(timezone.utc).isoformat()
    candidate = lambda ref, revision, content_hash: {
        "source_event_id": ref,
        "source_revision_id": revision,
        "source_content_hash": content_hash,
        "exact_source_ref": ref,
    }
    report = lambda candidates: json.dumps({"coverage": {"candidate_dispositions": candidates}})
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE consolidation_runs (
                run_id TEXT PRIMARY KEY, created_at TEXT, applied INTEGER,
                raw_candidate_count INTEGER, report_json TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO consolidation_runs VALUES (?, ?, 1, ?, ?)",
            [
                ("first", now, 2, report([candidate("raw:a", "rev-a", "hash-a"), candidate("raw:b", "rev-b", "hash-b")])),
                ("repeat", now, 1, report([candidate("raw:a", "rev-a", "hash-a")])),
            ],
        )
        conn.execute(
            """
            CREATE TABLE consolidation_coverage_receipts (
                run_id TEXT, source_event_id TEXT, source_revision_id TEXT,
                source_content_hash TEXT, exact_source_ref TEXT, covered_by TEXT,
                method_content_hash TEXT, mutation_id TEXT, created_at TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO consolidation_coverage_receipts VALUES (?, ?, ?, ?, ?, 'method.md', 'method-hash', ?, ?)",
            [
                ("first", "a", "rev-a", "hash-a", "raw:a", "mutation-1", now),
                ("repeat", "a", "rev-a", "hash-a", "raw:a", "mutation-2", now),
            ],
        )

    metric = consolidation_lineage_metric(
        db_path,
        freshness_window_seconds=30 * 86400,
        now=datetime.now(timezone.utc),
    )

    assert metric["denominator"] == 2
    assert metric["covered"] == 1
    assert metric["uncovered"] == 1
    assert metric["duplicate_receipt_count"] == 1
    assert metric["cold_start_state"] == "blocked"


def test_reference_auditor_recomputes_raw_candidate_and_real_wiki_target(tmp_path):
    from core.ops.cognitive_readiness_reference import build_consolidation_reference_audit

    database_dir = tmp_path / "db"
    database_dir.mkdir()
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    raw_refs = _create_raw_events_db(database_dir / "raw_events.db")
    method_bytes = b"# Trusted consolidation method\n"
    (wiki_dir / "method.md").write_bytes(method_bytes)
    method_hash = __import__("hashlib").sha256(method_bytes).hexdigest()
    frozen = _frozen_consolidation_report(
        raw_refs["active_revision"], content_hash=raw_refs["active_content_hash"]
    )
    with sqlite3.connect(database_dir / "cognitive_consolidation.db") as conn:
        conn.execute(
            """
            CREATE TABLE consolidation_runs (
                run_id TEXT PRIMARY KEY, applied INTEGER, report_json TEXT
            )
            """
        )
        conn.execute("INSERT INTO consolidation_runs VALUES ('run-1', 1, ?)", (frozen,))
        conn.execute(
            """
            CREATE TABLE consolidation_coverage_receipts (
                run_id TEXT, source_revision_id TEXT, source_content_hash TEXT,
                exact_source_ref TEXT, covered_by TEXT, method_content_hash TEXT,
                mutation_id TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO consolidation_coverage_receipts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                raw_refs["active_revision"],
                raw_refs["active_content_hash"],
                f"raw-revision:{raw_refs['active_revision']}",
                "method.md",
                method_hash,
                "mutation-1",
            ),
        )

    report = build_consolidation_reference_audit(database_dir=database_dir, wiki_dir=wiki_dir)

    assert report["ok"] is True
    assert report["candidate_denominator"] == report["covered"] == 1
    assert len(report["snapshot_hash"]) == 64

    with sqlite3.connect(database_dir / "cognitive_consolidation.db") as conn:
        conn.execute(
            "INSERT INTO consolidation_coverage_receipts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                raw_refs["active_revision"],
                raw_refs["active_content_hash"],
                f"raw-revision:{raw_refs['active_revision']}",
                "method.md",
                method_hash,
                "mutation-2",
            ),
        )
    duplicate = build_consolidation_reference_audit(database_dir=database_dir, wiki_dir=wiki_dir)
    assert duplicate["ok"] is False
    assert duplicate["duplicate_receipt_count"] == 1


def test_corrupt_or_old_required_schema_is_blocking(tmp_path):
    from core.ops.cognitive_readiness import build_cognitive_readiness_report

    database_dir = tmp_path / "db"
    database_dir.mkdir()
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (database_dir / "delivery_events.db").write_bytes(b"not-a-sqlite-database")
    (database_dir / "knowledge_graph.db").write_bytes(b"not-a-sqlite-database")

    report = build_cognitive_readiness_report(
        SimpleNamespace(database_dir=database_dir, wiki_dir=wiki_dir)
    )

    assert report["ok"] is False
    assert report["metrics"]["delivery_events"]["exists"] is False
    assert "error" in report["metrics"]["delivery_events"]
    assert "error" in report["metrics"]["knowledge_graph"]
    assert "knowledge_graph" in report["metrics"]["learning_signal"]["required_tables_missing"]
    assert "delivery_to_effect" in report["metrics"]["learning_signal"]["required_lineage_invalid"]
    assert "required_evidence_unavailable" in report["scorecard"]["blocking_findings"]

    (database_dir / "delivery_events.db").unlink()
    with sqlite3.connect(database_dir / "delivery_events.db") as conn:
        conn.execute("CREATE TABLE delivery_events (event_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE cognitive_outcomes (outcome_id TEXT PRIMARY KEY)")

    old_schema_report = build_cognitive_readiness_report(
        SimpleNamespace(database_dir=database_dir, wiki_dir=wiki_dir)
    )

    assert old_schema_report["ok"] is False
    assert old_schema_report["metrics"]["delivery_outcome_lineage"]["schema_valid"] is False
    assert (
        "delivery_to_effect"
        in old_schema_report["metrics"]["learning_signal"]["required_lineage_invalid"]
    )


def test_stale_lineage_evidence_degrades_readiness(tmp_path):
    from core.ops.cognitive_readiness import _observation_lineage_metric

    raw_db = tmp_path / "raw_events.db"
    observation_db = tmp_path / "observations.db"
    raw_refs = _create_raw_events_db(raw_db)
    with sqlite3.connect(observation_db) as conn:
        conn.execute(
            """
            CREATE TABLE observations (
                id TEXT PRIMARY KEY, source_type TEXT, source_id TEXT,
                created_at TEXT, updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO observations
            VALUES ('o1', 'raw', ?,
                    '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')
            """,
            (raw_refs["active_revision"],),
        )
    _record_observation_edge(raw_db, raw_refs["active_revision"], "o1")
    with sqlite3.connect(raw_db) as conn:
        conn.execute("UPDATE raw_provenance_edges SET created_at='2020-01-01T00:00:00+00:00'")

    metric = _observation_lineage_metric(
        raw_db,
        observation_db,
        freshness_window_seconds=30 * 86400,
        now=datetime.now(timezone.utc),
    )

    assert metric["covered"] == 1
    assert metric["freshness_state"] == "stale"
    assert metric["cold_start_state"] == "observed"


def test_full_lineage_with_invalid_timestamp_cannot_clear_freshness_budget(tmp_path):
    from core.ops.cognitive_readiness import _observation_lineage_metric

    raw_db = tmp_path / "raw_events.db"
    observation_db = tmp_path / "observations.db"
    raw_refs = _create_raw_events_db(raw_db)
    with sqlite3.connect(observation_db) as conn:
        conn.execute(
            """
            CREATE TABLE observations (
                id TEXT PRIMARY KEY, source_type TEXT, source_id TEXT,
                created_at TEXT, updated_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO observations VALUES ('o1', 'raw', ?, 'bad-time', 'bad-time')",
            (raw_refs["active_revision"],),
        )
    _record_observation_edge(raw_db, raw_refs["active_revision"], "o1")
    with sqlite3.connect(raw_db) as conn:
        conn.execute("UPDATE raw_provenance_edges SET created_at='bad-time'")

    metric = _observation_lineage_metric(raw_db, observation_db)

    assert metric["coverage_ratio"] == 1.0
    assert metric["freshness_state"] == "unavailable"


def test_doctor_cognitive_readiness_json_branch(fake_config, capsys, monkeypatch):
    import mnemos_cli

    monkeypatch.setattr("core.cli.commands.doctor._get_config", lambda: fake_config)

    args = argparse.Namespace(
        doctor_action=None,
        agent_name="",
        e2e=False,
        json=True,
        verbose=False,
        cognitive_readiness=True,
    )

    result = mnemos_cli.cmd_doctor(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert result is False
    assert payload["schema_version"] == "mnemos.cognitive_readiness.v2"
    assert payload["metrics"]["page_metrics"]["source_count_zero"] == 1


def test_doctor_parser_accepts_cognitive_readiness_json_flags():
    import mnemos_cli

    args = mnemos_cli.build_parser().parse_args(["doctor", "--cognitive-readiness", "--json"])

    assert args.command == "doctor"
    assert args.cognitive_readiness is True
    assert args.json is True


def test_audit_cognitive_readiness_budget_cli_returns_nonzero(fake_config, capsys):
    from scripts.audit_cognitive_readiness import main

    rc = main(
        [
            "--database-dir",
            str(fake_config.database_dir),
            "--wiki-dir",
            str(fake_config.wiki_dir),
            "--raw-vault-dir",
            str(fake_config.obsidian_vault_path),
            "--json",
            "--budget",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["ok"] is False
    assert payload["budget"]["failure_count"] > 0


def test_cognitive_readiness_sql_helpers_reject_unapproved_identifiers():
    from core.ops import cognitive_readiness

    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE sample (status TEXT, source_refs TEXT)")

        with pytest.raises(ValueError):
            cognitive_readiness._count(conn, "sample; DROP TABLE sample")
        with pytest.raises(ValueError):
            cognitive_readiness._count_where(conn, "sample", "1 = 1")
        with pytest.raises(ValueError):
            cognitive_readiness._group_counts(conn, "sample", "status; DROP")
        with pytest.raises(ValueError):
            cognitive_readiness._json_nonempty_count(
                conn,
                "sample",
                "source_refs; DROP",
                {"source_refs"},
            )


def test_search_sessions_record_click_open_and_ignore(tmp_path):
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from core.app.context_search import ContextAwareSearch
    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
    from core.scoring.subject_provenance import record_scoring_subject_provenance

    db_path = tmp_path / "mnemos.db"
    principal = PrincipalEnvelope(
        principal_id="test:readiness-search",
        agent="codex",
        host_kind="test",
        capability_id="readiness-search",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    narrowing = AccessNarrowing(
        session_id="readiness-search-session",
        project="mnemos",
    )
    access_control = make_cognitive_access_envelope(
        owner_principal_id=principal.principal_id,
        owner_agent=principal.agent,
        scope_type="session",
        scope_id=narrowing.session_id,
        session_id=narrowing.session_id,
        project=narrowing.project,
        purposes=(
            "cognitive_state_read",
            "cognitive_state_write",
            "score_training",
            "search_feedback",
        ),
        consent_provenance_refs=("readiness-search-consent",),
        sensitivity="sensitive",
        retention_policy="test",
        source_acl_lineage=("sha256:" + "a" * 64,),
        visibility="private",
    )
    AdaptiveScorerV2.ensure_tables(str(db_path))
    from core.cognitive.state_schema import initialize_cognitive_state_schema

    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(search_sessions)")}
        assert {"opened_path", "opened_at", "ignored_at", "outcome_status", "outcome_at"} <= columns
        click_cursor = conn.execute(
            """
            INSERT INTO search_sessions (session_id, query, result_paths, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("s-click", "query", '["03-Tech/x.md"]', datetime.now().isoformat()),
        )
        ignore_cursor = conn.execute(
            """
            INSERT INTO search_sessions (session_id, query, result_paths, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("s-ignore", "query2", '["03-Tech/y.md"]', datetime.now().isoformat()),
        )
        for object_id in (click_cursor.lastrowid, ignore_cursor.lastrowid):
            record_scoring_subject_provenance(
                conn,
                object_type="search_session",
                object_id=str(object_id),
                subject_provenance=access_control,
            )

    assert ContextAwareSearch.record_search_click(
        "03-Tech/x.md",
        db_path=db_path,
        principal=principal,
        narrowing=narrowing,
    )
    assert ContextAwareSearch.record_search_ignore(
        "s-ignore",
        db_path=db_path,
        principal=principal,
        narrowing=narrowing,
    )

    with sqlite3.connect(db_path) as conn:
        click = conn.execute(
            "SELECT clicked_path, opened_path, outcome_status FROM search_sessions WHERE session_id='s-click'"
        ).fetchone()
        ignored = conn.execute(
            "SELECT ignored_at, outcome_status FROM search_sessions WHERE session_id='s-ignore'"
        ).fetchone()
        legacy_training_tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if str(row[0])
            in {
                "ground_truth_signals",
                "scorer_training_queue",
                "scorer_feedback_events",
                "scorer_models",
            }
        }
    assert click == (None, None, "")
    assert ignored == (None, "")
    assert legacy_training_tables == set()
    from core.cognitive.state_store import CognitiveStateStore

    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    assert len(state.current_revisions(object_type="user_reaction_event")) == 2


def test_search_session_no_result_creates_outcome_schema(tmp_path, monkeypatch):
    import core.config
    import core.app.context_search as context_search
    from core.app.context_search import ContextAwareSearch

    fake_config = SimpleNamespace(database_dir=tmp_path, wiki_dir=tmp_path / "wiki")
    fake_config.get = lambda key, default=None: default
    monkeypatch.setattr(
        core.config,
        "get_config",
        lambda: fake_config,
    )
    monkeypatch.setattr(
        context_search,
        "get_config",
        lambda: fake_config,
    )

    search = ContextAwareSearch()
    search._record_search_session("query-without-results", [])

    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(search_sessions)")}
        row = conn.execute(
            """
            SELECT query, result_paths, outcome_status
            FROM search_sessions
            """
        ).fetchone()

    assert {"opened_path", "ignored_at", "outcome_status", "outcome_at"} <= columns
    assert row == ("query-without-results", "[]", "no_result")
