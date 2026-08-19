# -*- coding: utf-8 -*-
"""Tests for daemon.process_control helpers."""

from __future__ import annotations

import os
from types import SimpleNamespace

from daemon import process_control


def test_pid_lock_roundtrip(tmp_path):
    pid_file = tmp_path / "daemon.pid"

    record = {
        "schema_version": "mnemos.daemon_instance.v2",
        "instance_id": "instance-1",
        "pid": os.getpid(),
    }

    assert process_control.acquire_pid_lock(pid_file, instance_record=record) is True
    assert process_control.read_pid(pid_file) == os.getpid()
    assert process_control.read_pid_record(pid_file) == record
    assert pid_file.stat().st_mode & 0o777 == 0o600

    process_control.release_pid_lock(pid_file)
    assert not pid_file.exists()


def test_second_pid_lock_fails_while_held(tmp_path):
    pid_file = tmp_path / "daemon.pid"
    record = {
        "schema_version": "mnemos.daemon_instance.v2",
        "instance_id": "instance-1",
        "pid": os.getpid(),
    }

    assert process_control.acquire_pid_lock(pid_file, instance_record=record) is True
    try:
        assert process_control.acquire_pid_lock(pid_file, instance_record=record) is False
    finally:
        process_control.release_pid_lock(pid_file)


def test_startup_status_roundtrip(tmp_path):
    status_file = tmp_path / "daemon.status"

    process_control.write_startup_status(status_file, True)
    success, pid, error = process_control.read_startup_status(status_file, timeout=0.1)

    assert success is True
    assert pid == os.getpid()
    assert error == ""

    process_control.clear_startup_status(status_file)
    assert not status_file.exists()


def test_legacy_pid_file_is_parsed_but_explicitly_untrusted(tmp_path):
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("12345", encoding="utf-8")

    record = process_control.read_pid_record(pid_file)

    assert record == {
        "schema_version": "mnemos.daemon_pid.legacy",
        "pid": 12345,
        "identity_complete": False,
    }


def test_empty_offline_migration_lock_has_no_daemon_identity(
    tmp_path,
    caplog,
):
    pid_file = tmp_path / "daemon.pid"
    pid_file.touch(mode=0o600)

    assert process_control.read_pid_record(pid_file) is None
    assert process_control.read_pid(pid_file) is None
    assert "读取 PID 文件失败" not in caplog.text


def test_windows_pid_liveness_requires_exact_csv_pid(monkeypatch):
    monkeypatch.setattr(process_control.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        process_control.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout='"python.exe","142","Console","1","1,000 K"\n',
        ),
    )

    assert process_control.pid_exists(42) is False
    assert process_control.pid_exists(142) is True


def test_pid_lock_rejects_incomplete_new_identity(tmp_path):
    pid_file = tmp_path / "daemon.pid"

    assert process_control.acquire_pid_lock(
        pid_file,
        instance_record={"pid": os.getpid()},
    ) is False
    assert not pid_file.exists()
