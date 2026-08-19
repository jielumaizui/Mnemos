import os

from core.sync_framework.file_watcher import StatFileWatcher


def test_modified_change_preserves_previous_stat_contract(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("old", encoding="utf-8")
    os.utime(path, (1000, 1000))

    watcher = StatFileWatcher(tmp_path)
    watcher.prime()

    path.write_text("updated content", encoding="utf-8")
    os.utime(path, (2000, 2000))

    changes = watcher.scan()

    assert len(changes) == 1
    change = changes[0]
    assert change.change_type == "modified"
    assert change.old_mtime == 1000
    assert change.new_mtime == 2000
    assert change.old_size == 3
    assert change.new_size == len("updated content")
