from types import SimpleNamespace


def test_signal_health_uses_four_core_sources_and_marks_fs_optional(tmp_path):
    from core.persona.psyche import SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "signals.db")
    health = store.get_signal_health()

    assert set(health) == {"session", "git", "wiki", "notes", "file_system"}
    assert health["file_system"]["optional"] is True
    assert "wechat" not in health


def test_signal_store_handle_event_records_core_sources(tmp_path):
    from core.persona.psyche import SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "signals.db")
    session_id = store.handle_event(
        "session_completed",
        {
            "session_id": "s1",
            "task_type": "coding",
            "duration_seconds": 600,
        },
    )
    wiki_id = store.handle_event(
        "wiki_page_accessed",
        {
            "page_path": "a.md",
            "action_type": "access",
        },
    )
    notes_id = store.handle_event(
        "notes_synced",
        {
            "note_uid": "n1",
            "content_length": 300,
            "tags": ["dev"],
        },
    )

    assert session_id > 0
    assert wiki_id > 0
    assert notes_id > 0
    assert store.get_signal_stats()["session"] == 1
    assert store.get_signal_stats()["knowledge"] == 1
    assert store.get_signal_stats()["notes"] == 1


def test_preference_analyzer_defaults_incremental_and_falls_back_to_metis(tmp_path):
    from core.persona.psyche import SignalStore
    from core.persona.pythia import PreferenceAnalyzer

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "signals.db")
    metis = SimpleNamespace(
        domain_entropy=0.2,
        learning_mode={"simple_mode": "方法论导向型"},
        tool_stack=[("python", 3), ("sqlite", 2), ("pytest", 2), ("docker", 1)],
    )

    profile = PreferenceAnalyzer(store=store).analyze(metis_profile=metis)

    assert profile.signal_count == 0
    assert profile.value.depth_vs_breadth == 0.8
    assert profile.cognitive.deduction == 0.7
    assert profile.cognitive.system_view == 0.4
    assert "focus_depth" in profile.energy.insufficient_dimensions
