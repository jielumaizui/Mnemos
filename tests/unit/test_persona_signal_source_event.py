from __future__ import annotations

import sqlite3


def test_reflection_signal_source_event_is_idempotent(tmp_path):
    from core.persona.psyche import SignalStore

    db_path = tmp_path / "user_signals.db"
    store = SignalStore(initialize_schema=True, db_path=db_path)

    first = store.add_signal(
        "retrospective_lesson",
        "先验证再提交",
        0.9,
        "recap:one",
        source_event_id="recap-command-one",
    )
    duplicate = store.add_signal(
        "retrospective_lesson",
        "先验证再提交",
        0.9,
        "recap:one",
        source_event_id="recap-command-one",
    )
    store.close()

    assert duplicate == first
    with sqlite3.connect(str(db_path)) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM reflection_signals WHERE source_event_id=?",
                ("recap-command-one",),
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM profile_signals").fetchone()[0] == 0


def test_reflection_signal_never_creates_an_unscoped_profile_effect(tmp_path):
    from core.persona.psyche import SignalStore

    db_path = tmp_path / "user_signals.db"
    store = SignalStore(initialize_schema=True, db_path=db_path)

    store.add_signal(
        "retrospective_lesson",
        "反思只能作为后续授权分析的输入，不能直接改写用户画像",
        0.9,
        "recap:atomic",
        source_event_id="recap-command-atomic",
    )

    with sqlite3.connect(str(db_path)) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM reflection_signals WHERE source_event_id=?",
                ("recap-command-atomic",),
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM profile_signals").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM profile_assertions").fetchone()[0] == 0
    store.close()
