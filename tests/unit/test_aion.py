from datetime import datetime, timedelta


def _write_page(path, frontmatter):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n# {path.stem}\n", encoding="utf-8")


def test_scan_auto_reminders_covers_vault_and_contract_fields(tmp_path):
    from core.kia.aion import TimeCapsule

    created = datetime.now().strftime("%Y-%m-%d")
    page = tmp_path / "03-Tech" / "python.md"
    excluded = tmp_path / "99-Reports" / "report.md"
    _write_page(page, f"temporal_scope: custom\ncreated_at: {created}\n")
    _write_page(excluded, f"temporal_scope: custom\ncreated_at: {created}\n")

    capsule = TimeCapsule(
        wiki_base=str(tmp_path),
        db_path=str(tmp_path / "capsule.db"),
        auto_reminder_days={"custom": [30]},
    )

    assert capsule.scan_for_auto_reminders() == 1
    reminders = capsule.get_all_reminders()
    assert len(reminders) == 1
    assert reminders[0].page_path == str(page)


def test_dedupe_by_page_and_date_merges_reason(tmp_path):
    from core.kia.aion import TimeCapsule

    capsule = TimeCapsule(wiki_base=str(tmp_path), db_path=str(tmp_path / "capsule.db"))

    assert capsule._add_reminder("p.md", "p", "auto_expiry", "2026-06-01", "reason-a") is True
    assert capsule._add_reminder("p.md", "p", "auto_version", "2026-06-01", "reason-b") is False

    reminders = capsule.get_all_reminders()
    assert len(reminders) == 1
    assert "reason-a" in reminders[0].reason
    assert "reason-b" in reminders[0].reason


def test_generate_periodic_reminders_weekly_monthly_quarterly(tmp_path):
    from core.kia.aion import TimeCapsule

    capsule = TimeCapsule(wiki_base=str(tmp_path), db_path=str(tmp_path / "capsule.db"))

    count = capsule.generate_periodic_reminders(datetime(2026, 6, 1))

    assert count == 2
    titles = {r.page_title for r in capsule.get_all_reminders()}
    assert titles == {"每周知识回顾", "每月知识回顾"}

    quarter_count = capsule.generate_periodic_reminders(datetime(2026, 4, 1))
    assert quarter_count == 2
    titles = {r.page_title for r in capsule.get_all_reminders()}
    assert "季度知识回顾" in titles


def test_publish_due_and_overdue_events(tmp_path, monkeypatch):
    from core.kia.aion import TimeCapsule
    import core.mnemos_bus as bus

    published = []
    monkeypatch.setattr(bus, "publish_event", lambda event_type, agent, payload: published.append((event_type, agent, payload)) or "trace")

    capsule = TimeCapsule(wiki_base=str(tmp_path), db_path=str(tmp_path / "capsule.db"))
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    capsule._add_reminder("due.md", "due", "manual_review", today, "due reason")
    capsule._add_reminder("old.md", "old", "manual_review", yesterday, "old reason")

    assert capsule.publish_due_events(days_ahead=0) == 1
    assert capsule.publish_overdue_events() == 1

    assert published[0][0] == "capsule.due"
    assert published[0][2]["page_path"] == "due.md"
    assert published[1][0] == "capsule.overdue"
    assert published[1][2]["days_overdue"] >= 1
