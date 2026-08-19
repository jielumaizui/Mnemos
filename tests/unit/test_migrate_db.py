from pathlib import Path

from scripts import migrate_db


def test_rollback_prints_requested_target_version(capsys):
    """--rollback VERSION 是人工恢复指引的一部分，输出应保留目标版本。"""
    db_path = Path("/tmp/mnemos-test-sync-log.db")

    migrate_db.rollback(db_path=db_path, target_version=3)

    captured = capsys.readouterr()
    assert "目标 Schema 版本: v3" in captured.out
    assert f"找到备份文件: {db_path}.bak.*" in captured.out
