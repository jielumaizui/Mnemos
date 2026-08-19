"""Tests for distill_failed cleanup."""

import os
import time

from core.hephaestus.distillation_failure import cleanup_failed_distill
from core.ops.operational_incident import initialize_operational_incident_schema


def test_cleanup_failed_distill_removes_old_files(tmp_path):
    """超过 TTL 的失败文件应被删除"""
    initialize_operational_incident_schema(tmp_path / "operational_incidents.db")
    failed_dir = tmp_path / "distill_failed"
    failed_dir.mkdir()

    old_file = failed_dir / "failed-s1-20260101-000000.json"
    old_file.write_text("{}", encoding="utf-8")
    # 40 天前
    old_mtime = time.time() - 40 * 86400
    os.utime(old_file, (old_mtime, old_mtime))

    new_file = failed_dir / "failed-s2-20260625-000000.json"
    new_file.write_text("{}", encoding="utf-8")

    result = cleanup_failed_distill(tmp_path, ttl_days=30, max_count=1000)

    assert result["removed"] == 1
    assert not old_file.exists()
    assert new_file.exists()


def test_cleanup_failed_distill_respects_max_count(tmp_path):
    """文件数量超过上限时，按时间删除最旧的"""
    initialize_operational_incident_schema(tmp_path / "operational_incidents.db")
    failed_dir = tmp_path / "distill_failed"
    failed_dir.mkdir()

    files = []
    for i in range(5):
        path = failed_dir / f"failed-s{i}-20260625-000000.json"
        path.write_text("{}", encoding="utf-8")
        # i 越小越旧
        mtime = time.time() - (5 - i) * 3600
        os.utime(path, (mtime, mtime))
        files.append(path)

    result = cleanup_failed_distill(tmp_path, ttl_days=30, max_count=2)

    assert result["removed"] == 3
    assert not files[0].exists()
    assert not files[1].exists()
    assert not files[2].exists()
    assert files[3].exists()
    assert files[4].exists()
