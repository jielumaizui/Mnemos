from pathlib import Path

import pytest

from core.sync_framework.agent_path_watcher import AgentPathWatcher
from core.sync_framework.file_watcher import (
    FileWatcherUnavailableError,
    StatFileWatcher,
)


def test_stat_file_watcher_reports_added_modified_deleted_without_reading_body(tmp_path):
    note = tmp_path / "note.md"
    watcher = StatFileWatcher(tmp_path)

    assert watcher.scan() == []

    note.write_text("first", encoding="utf-8")
    added = watcher.scan()
    assert len(added) == 1
    assert added[0].change_type == "added"
    assert added[0].path == str(note)

    note.write_text("second content", encoding="utf-8")
    modified = watcher.scan()
    assert len(modified) == 1
    assert modified[0].change_type == "modified"

    note.unlink()
    deleted = watcher.scan()
    assert len(deleted) == 1
    assert deleted[0].change_type == "deleted"


def test_agent_path_watcher_refreshes_path_state_only(tmp_path):
    agent_dir = tmp_path / "codex"

    def discover(agent: str) -> Path | None:
        assert agent == "codex"
        return agent_dir

    watcher = AgentPathWatcher(["codex"], discoverer=discover)

    missing = watcher.refresh()
    assert missing[0].agent == "codex"
    assert missing[0].exists is False
    assert missing[0].changed is True

    agent_dir.mkdir()
    changed = watcher.refresh()
    assert changed[0].exists is True
    assert changed[0].path == str(agent_dir)

    unchanged = watcher.refresh()
    assert unchanged == []


def test_agent_path_watcher_prefers_known_transcript_subdir(tmp_path):
    agent_dir = tmp_path / "codex"
    sessions_dir = agent_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    watcher = AgentPathWatcher(["codex"], discoverer=lambda _agent: agent_dir)

    changed = watcher.refresh()
    assert changed[0].exists is True
    assert changed[0].path == str(sessions_dir)


def test_agent_path_watcher_preserves_unavailable_as_a_third_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "codex"
    original_stat = Path.stat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == agent_dir:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    watcher = AgentPathWatcher(["codex"], discoverer=lambda _agent: agent_dir)

    state = watcher.refresh()[0]

    assert state.exists is False
    assert state.availability_state == "unavailable"
    assert state.error_code == "agent_path_inspection_unavailable"


def test_stat_file_watcher_does_not_invent_deletions_when_root_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = tmp_path / "note.md"
    note.write_text("present", encoding="utf-8")
    watcher = StatFileWatcher(tmp_path)
    watcher.prime()
    original_stat = Path.stat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == tmp_path:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(
        FileWatcherUnavailableError,
        match="file_watcher_root_unavailable",
    ):
        watcher.scan()

    assert str(note) in watcher._snapshot
