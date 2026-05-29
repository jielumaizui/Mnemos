import time
from pathlib import Path

from core.sync_framework.agent_source import SessionInfo
from mnemos_daemon import _L1ScanState, _select_l1_sessions


def _session(path: Path, session_id: str) -> SessionInfo:
    stat = path.stat()
    return SessionInfo(
        session_id=session_id,
        source_path=path,
        working_dir=str(path.parent),
        mtime=stat.st_mtime,
    )


def test_l1_session_selection_limits_and_skips_unsafe_files(tmp_path):
    now = time.time()
    fresh_a = tmp_path / "fresh-a.jsonl"
    fresh_b = tmp_path / "fresh-b.jsonl"
    fresh_c = tmp_path / "fresh-c.jsonl"
    stale = tmp_path / "stale.jsonl"
    huge = tmp_path / "huge.jsonl"

    for path in (fresh_a, fresh_b, fresh_c, stale):
        path.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")
    huge.write_text("x" * 128, encoding="utf-8")

    old_mtime = now - 3 * 3600
    stale.touch()
    # os.utime is available through Path's underlying string path.
    import os
    os.utime(stale, (old_mtime, old_mtime))

    sessions = [
        _session(fresh_a, "a"),
        _session(fresh_b, "b"),
        _session(fresh_c, "c"),
        _session(stale, "stale"),
        _session(huge, "huge"),
    ]
    limits = {
        "max_sessions_per_source": 2,
        "max_file_bytes": 64,
        "recent_hours": 1,
    }
    state = _L1ScanState(tmp_path / "l1_state.json")

    selected, stats = _select_l1_sessions("claude", sessions, state, limits)

    assert len(selected) == 2
    assert stats["discovered"] == 5
    assert stats["skipped_stale"] == 1
    assert stats["skipped_large"] == 1
    assert stats["skipped_over_limit"] == 1


def test_l1_scan_state_prevents_repeated_unchanged_scan(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")
    session = _session(path, "sess")
    limits = {
        "max_sessions_per_source": 5,
        "max_file_bytes": 1024,
        "recent_hours": 24,
    }

    state_path = tmp_path / "l1_state.json"
    state = _L1ScanState(state_path)
    selected, stats = _select_l1_sessions("codex", [session], state, limits)
    assert len(selected) == 1
    state.mark_scanned("codex", session, selected[0][1], "scanned")
    state.save()

    reloaded = _L1ScanState(state_path)
    selected_again, stats_again = _select_l1_sessions("codex", [session], reloaded, limits)

    assert selected_again == []
    assert stats_again["skipped_unchanged"] == 1
