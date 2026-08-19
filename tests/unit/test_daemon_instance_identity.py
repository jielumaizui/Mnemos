# -*- coding: utf-8 -*-
"""Contracts for daemon instance identity and signal safety."""

from __future__ import annotations

import signal
from datetime import datetime, timezone

import pytest

from daemon import instance_identity


def _fingerprint(*, start_token: str = "start-1") -> instance_identity.ProcessFingerprint:
    return instance_identity.ProcessFingerprint(
        pid=4242,
        pid_start_time=start_token,
        boot_id="boot-1",
        executable="/usr/bin/python3",
        command_line="/usr/bin/python3 /repo/mnemos_daemon.py start",
    )


def _record(tmp_path, *, start_token: str = "start-1") -> dict:
    config_file = tmp_path / "configs" / "main.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text('{"daemon":{"enabled":true}}', encoding="utf-8")
    return instance_identity.create_instance_record(
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        process_fingerprint=_fingerprint(start_token=start_token),
        instance_id="instance-1",
        created_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        build_commit="commit-1",
        config_fingerprint="sha256:effective-config",
    )


def test_instance_record_binds_process_build_config_database_and_services(tmp_path):
    record = _record(tmp_path)

    assert record["schema_version"] == "mnemos.daemon_instance.v2"
    assert record["instance_id"] == "instance-1"
    assert record["pid"] == 4242
    assert record["pid_start_time"] == "start-1"
    assert record["boot_id"] == "boot-1"
    assert record["executable"] == "/usr/bin/python3"
    assert record["python"]
    assert record["commit"] == "commit-1"
    assert record["build_fingerprint"].startswith("sha256:")
    assert record["config_hash"].startswith("sha256:")
    assert record["config_fingerprint"] == "sha256:effective-config"
    assert record["database_identity"].startswith("sha256:")
    assert record["service_manifest"] == ["capture_worker", "heartbeat"]
    assert record["service_manifest_hash"].startswith("sha256:")


def test_instance_record_fails_closed_when_effective_config_cannot_be_fingerprinted(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        instance_identity,
        "_effective_config_fingerprint",
        lambda: (_ for _ in ()).throw(RuntimeError("invalid config")),
    )

    with pytest.raises(RuntimeError, match="invalid config"):
        instance_identity.create_instance_record(
            database_dir=tmp_path,
            service_names=("heartbeat",),
            project_root=tmp_path,
            process_fingerprint=_fingerprint(),
        )


def test_verify_rejects_pid_reuse_start_token_mismatch(tmp_path):
    record = _record(tmp_path)

    result = instance_identity.verify_instance_record(
        record,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        require_current_context=False,
        process_inspector=lambda _pid: _fingerprint(start_token="start-2"),
    )

    assert result.ok is False
    assert result.identity_match is False
    assert result.reason == "pid_start_time_mismatch"


def test_verify_tolerates_darwin_boot_time_jitter_but_not_a_reboot(tmp_path):
    record = _record(tmp_path)
    record["boot_id"] = "darwin-boot:100"

    jitter = instance_identity.verify_instance_record(
        record,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        require_current_context=False,
        process_inspector=lambda _pid: instance_identity.ProcessFingerprint(
            4242,
            "start-1",
            "darwin-boot:101",
            "/usr/bin/python3",
            "/usr/bin/python3 /repo/mnemos_daemon.py start",
        ),
    )
    reboot = instance_identity.verify_instance_record(
        record,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        require_current_context=False,
        process_inspector=lambda _pid: instance_identity.ProcessFingerprint(
            4242,
            "start-1",
            "darwin-boot:110",
            "/usr/bin/python3",
            "/usr/bin/python3 /repo/mnemos_daemon.py start",
        ),
    )

    assert jitter.ok is True
    assert reboot.reason == "boot_id_mismatch"


def test_verify_uses_darwin_boot_session_uuid_when_available(tmp_path):
    record = _record(tmp_path)
    record["boot_id"] = "darwin-session:session-a|bootsec:100"

    same_session = instance_identity.verify_instance_record(
        record,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        require_current_context=False,
        process_inspector=lambda _pid: instance_identity.ProcessFingerprint(
            4242,
            "start-1",
            "darwin-session:session-a|bootsec:105",
            "/usr/bin/python3",
            "/usr/bin/python3 /repo/mnemos_daemon.py start",
        ),
    )
    different_session = instance_identity.verify_instance_record(
        record,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        require_current_context=False,
        process_inspector=lambda _pid: instance_identity.ProcessFingerprint(
            4242,
            "start-1",
            "darwin-session:session-b|bootsec:100",
            "/usr/bin/python3",
            "/usr/bin/python3 /repo/mnemos_daemon.py start",
        ),
    )

    assert same_session.ok is True
    assert different_session.reason == "boot_id_mismatch"


def test_verify_rejects_runtime_code_drift_for_status_and_health(tmp_path):
    record = _record(tmp_path)

    result = instance_identity.verify_instance_record(
        record,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        require_current_context=True,
        process_inspector=lambda _pid: _fingerprint(),
        current_context={
            **instance_identity.build_runtime_context(
                database_dir=tmp_path,
                service_names=("heartbeat", "capture_worker"),
                project_root=tmp_path / "repo",
                config_fingerprint="sha256:effective-config",
            ),
            "commit": "commit-2",
            "build_fingerprint": "sha256:different-runtime-code",
        },
    )

    assert result.ok is False
    assert result.identity_match is True
    assert result.reason == "build_fingerprint_mismatch"


def test_verify_allows_doc_only_commit_drift_when_runtime_code_matches(tmp_path):
    record = _record(tmp_path)
    context = instance_identity.build_runtime_context(
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        config_fingerprint="sha256:effective-config",
    )
    context["commit"] = "commit-2"

    result = instance_identity.verify_instance_record(
        record,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        require_current_context=True,
        process_inspector=lambda _pid: _fingerprint(),
        current_context=context,
    )

    assert result.ok is True
    assert result.details == {
        "context_match": True,
        "commit_match": False,
        "recorded_commit": "commit-1",
        "current_commit": "commit-2",
        "build_compatible": True,
    }


def test_verify_rejects_effective_config_drift_even_when_file_hash_matches(tmp_path):
    record = _record(tmp_path)
    context = instance_identity.build_runtime_context(
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        config_fingerprint="sha256:different-effective-config",
    )

    result = instance_identity.verify_instance_record(
        record,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        require_current_context=True,
        process_inspector=lambda _pid: _fingerprint(),
        current_context=context,
    )

    assert result.ok is False
    assert result.reason == "config_fingerprint_mismatch"


def test_signal_is_never_sent_when_instance_identity_mismatches(tmp_path):
    record = _record(tmp_path)
    sent: list[tuple[int, int]] = []

    result = instance_identity.signal_verified_instance(
        record,
        signal.SIGTERM,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        process_inspector=lambda _pid: _fingerprint(start_token="reused"),
        signal_sender=lambda pid, sig: sent.append((pid, sig)),
    )

    assert result.ok is False
    assert result.reason == "pid_start_time_mismatch"
    assert sent == []


def test_legacy_pid_migration_requires_os_proven_daemon_command(tmp_path):
    legacy = {
        "schema_version": "mnemos.daemon_pid.legacy",
        "pid": 4242,
        "identity_complete": False,
    }

    unrelated = instance_identity.migrate_historical_pid_for_control(
        legacy,
        database_dir=tmp_path,
        service_names=("heartbeat",),
        project_root=tmp_path,
        process_inspector=lambda _pid: instance_identity.ProcessFingerprint(
            4242, "start-1", "boot-1", "/usr/bin/python3", "python3 unrelated.py"
        ),
    )
    proven = instance_identity.migrate_historical_pid_for_control(
        legacy,
        database_dir=tmp_path,
        service_names=("heartbeat",),
        project_root=tmp_path,
        process_inspector=lambda _pid: _fingerprint(),
    )

    assert unrelated is None
    assert proven is not None
    assert proven["schema_version"] == "mnemos.daemon_instance.v2"
    assert proven["migration_source"] == "mnemos.daemon_pid.legacy"
    assert proven["migration_persisted"] is False


def test_v1_identity_migration_requires_all_os_process_facts_to_match(tmp_path):
    prior = _record(tmp_path)
    prior["schema_version"] = "mnemos.daemon_instance.v1"
    prior.pop("config_fingerprint")

    mismatch = instance_identity.migrate_historical_pid_for_control(
        prior,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        process_inspector=lambda _pid: _fingerprint(start_token="reused"),
    )
    migrated = instance_identity.migrate_historical_pid_for_control(
        prior,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        process_inspector=lambda _pid: _fingerprint(),
    )

    assert mismatch is None
    assert migrated is not None
    assert migrated["schema_version"] == "mnemos.daemon_instance.v2"
    assert migrated["migration_source"] == "mnemos.daemon_instance.v1"


def test_verify_rejects_incomplete_or_wrong_service_manifest(tmp_path):
    record = _record(tmp_path)
    record.pop("instance_id")

    incomplete = instance_identity.verify_instance_record(
        record,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker"),
        project_root=tmp_path / "repo",
        process_inspector=lambda _pid: _fingerprint(),
    )
    assert incomplete.reason == "missing_instance_id"

    record = _record(tmp_path)
    manifest_mismatch = instance_identity.verify_instance_record(
        record,
        database_dir=tmp_path,
        service_names=("heartbeat", "capture_worker", "eventbus"),
        project_root=tmp_path / "repo",
        process_inspector=lambda _pid: _fingerprint(),
    )
    assert manifest_mismatch.reason == "service_manifest_mismatch"


def test_darwin_inspection_uses_start_time_command_and_boot_identity(monkeypatch):
    outputs = {
        ("ps", "-o", "lstart=", "-p", "42"): "Fri Jul 10 16:00:00 2026\n",
        ("ps", "-ww", "-o", "command=", "-p", "42"): "/usr/bin/python3 /repo/mnemos_daemon.py start\n",
        ("ps", "-o", "comm=", "-p", "42"): "/usr/bin/python3\n",
        ("sysctl", "-n", "kern.bootsessionuuid"): "A55B73A1-02B1-4493-B17E-324C10AE3E0B\n",
        ("sysctl", "-n", "kern.boottime"): "{ sec = 1783600000, usec = 0 } Fri Jul 10\n",
    }

    class Result:
        def __init__(self, stdout: str):
            self.returncode = 0
            self.stdout = stdout

    monkeypatch.setattr(instance_identity.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(instance_identity.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(
        instance_identity.subprocess,
        "run",
        lambda args, **_kwargs: Result(outputs[tuple(args)]),
    )
    monkeypatch.setattr(instance_identity, "_darwin_proc_pidpath", lambda _pid: None)
    monkeypatch.setattr(instance_identity, "_darwin_proc_start_time", lambda _pid: None)

    observed = instance_identity.inspect_process(42)

    assert observed is not None
    assert observed.pid_start_time == "Fri Jul 10 16:00:00 2026"
    assert observed.boot_id == (
        "darwin-session:a55b73a1-02b1-4493-b17e-324c10ae3e0b|bootsec:1783600000"
    )
    assert "mnemos_daemon.py" in observed.command_line


def test_windows_inspection_does_not_use_posix_kill_probe(monkeypatch):
    expected = _fingerprint()
    monkeypatch.setattr(instance_identity.platform, "system", lambda: "Windows")
    monkeypatch.setattr(instance_identity, "_inspect_windows", lambda _pid: expected)
    monkeypatch.setattr(
        instance_identity.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(AssertionError("os.kill must not probe Windows")),
    )

    assert instance_identity.inspect_process(4242) == expected
