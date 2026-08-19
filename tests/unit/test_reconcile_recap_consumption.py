from __future__ import annotations

import sqlite3


def test_reconcile_recap_consumption_is_dry_run_by_default_and_apply_is_idempotent(
    tmp_path,
):
    database_dir = tmp_path / "db"
    wiki_dir = tmp_path / "wiki"
    backup_dir = tmp_path / "backups"
    database_dir.mkdir()
    wiki_dir.mkdir()
    recap_db = database_dir / "recap_tasks.db"
    with sqlite3.connect(str(recap_db)) as conn:
        conn.execute(
            "CREATE TABLE recap_tasks(task_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO recap_tasks VALUES ('task-1', 'resolved')")
        conn.execute(
            """
            CREATE TABLE recap_feedback_events(
                event_id TEXT PRIMARY KEY,
                recap_id TEXT NOT NULL,
                feedback_type TEXT NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO recap_feedback_events VALUES ('old-1', 'recap-1', 'useful', '', '2026-07-01')"
        )
        conn.execute(
            """
            CREATE TABLE recap_consumption_plans(
                recap_id TEXT PRIMARY KEY,
                targets TEXT NOT NULL,
                activation_rules TEXT NOT NULL,
                consume_priority TEXT NOT NULL,
                follow_up_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO recap_consumption_plans
            VALUES ('recap-1', '["wiki_search"]', '{}', 'medium', '', '2026-07-01')
            """
        )
    user_db = database_dir / "user_signals.db"
    from core.persona.psyche import SignalStore

    SignalStore(db_path=user_db, initialize_schema=True).close()
    policy_db = database_dir / "policy_patches.db"
    reminder_db = database_dir / "dialog_reminder.db"
    sqlite3.connect(str(policy_db)).close()
    sqlite3.connect(str(reminder_db)).close()

    from scripts.reconcile_recap_consumption import reconcile

    dry_run = reconcile(
        database_dir=database_dir,
        wiki_dir=wiki_dir,
        backup_root=backup_dir,
        apply=False,
    )

    assert dry_run["apply"] is False
    assert dry_run["schema_changes_required"] == 3
    assert dry_run["historical_unknown_count"] == 1
    with sqlite3.connect(str(recap_db)) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(recap_consumption_plans)")
        }
        assert "plan_id" not in columns

    from core.cognitive.material_effect_schema import (
        reconcile_material_effect_schema,
    )

    for target_db in (user_db, policy_db):
        with sqlite3.connect(str(target_db)) as conn:
            reconciled = reconcile_material_effect_schema(conn, apply=True)
            assert reconciled["ok"] is True

    applied = reconcile(
        database_dir=database_dir,
        wiki_dir=wiki_dir,
        backup_root=backup_dir,
        apply=True,
    )

    assert applied["ok"] is True
    assert applied["backed_up"] == [
        "recap_tasks.db",
        "user_signals.db",
        "policy_patches.db",
        "dialog_reminder.db",
    ]
    assert applied["integrity"] == {
        "recap_tasks.db": "ok",
        "user_signals.db": "ok",
        "policy_patches.db": "ok",
        "dialog_reminder.db": "ok",
    }
    with sqlite3.connect(str(recap_db)) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='recap_consumption_receipts'"
        ).fetchone()
        assert conn.execute(
            "SELECT COUNT(*) FROM recap_feedback_events_legacy_root010"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM recap_consumption_plans_legacy_root010"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM recap_feedback_events").fetchone()[0] == 0
    with sqlite3.connect(str(user_db)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(reflection_signals)")}
    assert "source_event_id" in columns

    stable = reconcile(
        database_dir=database_dir,
        wiki_dir=wiki_dir,
        backup_root=backup_dir,
        apply=False,
    )
    assert stable["schema_changes_required"] == 0
