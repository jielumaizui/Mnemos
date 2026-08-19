# -*- coding: utf-8 -*-
"""Tests for high-level daemon instance lifecycle control."""

from __future__ import annotations

import signal

from daemon import instance_control, instance_identity


def _record() -> dict:
    return {
        "schema_version": instance_identity.SCHEMA_VERSION,
        "instance_id": "instance-1",
        "pid": 4242,
    }


def _result(ok: bool, reason: str) -> instance_identity.VerificationResult:
    return instance_identity.VerificationResult(
        ok=ok,
        reason=reason,
        identity_match=ok,
        pid=4242,
        instance_id="instance-1",
    )


def test_stop_refuses_unproven_legacy_pid_without_signal(monkeypatch, tmp_path):
    legacy = {"schema_version": "mnemos.daemon_pid.legacy", "pid": 4242}
    signals = []
    monkeypatch.setattr(instance_control.process_control, "read_pid_record", lambda *_a, **_k: legacy)
    monkeypatch.setattr(instance_control.process_control, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        instance_control.instance_identity,
        "migrate_historical_pid_for_control",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        instance_control.instance_identity,
        "signal_verified_instance",
        lambda *_args, **_kwargs: signals.append(True),
    )

    result = instance_control.stop(
        tmp_path / "daemon.pid",
        database_dir=tmp_path,
        service_names=("heartbeat",),
        project_root=tmp_path,
    )

    assert result.exit_code == 1
    assert signals == []


def test_stop_refuses_reused_pid_before_any_signal(monkeypatch, tmp_path):
    record = _record()
    signals = []
    monkeypatch.setattr(instance_control.process_control, "read_pid_record", lambda *_a, **_k: record)
    monkeypatch.setattr(instance_control.process_control, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        instance_control,
        "_verify",
        lambda *_args, **_kwargs: _result(False, "pid_start_time_mismatch"),
    )
    monkeypatch.setattr(
        instance_control.instance_identity,
        "signal_verified_instance",
        lambda *_args, **_kwargs: signals.append(True),
    )

    result = instance_control.stop(
        tmp_path / "daemon.pid",
        database_dir=tmp_path,
        service_names=("heartbeat",),
        project_root=tmp_path,
    )

    assert result.exit_code == 1
    assert signals == []


def test_stop_sends_only_sigterm_when_original_instance_exits(monkeypatch, tmp_path):
    record = _record()
    checks = iter(
        [_result(True, "verified"), _result(False, "process_not_found_or_unverifiable")]
    )
    signals = []
    cleared = []
    monkeypatch.setattr(instance_control.process_control, "read_pid_record", lambda *_a, **_k: record)
    monkeypatch.setattr(instance_control, "_verify", lambda *_a, **_k: next(checks))
    monkeypatch.setattr(instance_control.platform, "system", lambda: "Linux")
    monkeypatch.setattr(instance_control.process_control, "pid_exists", lambda _pid: False)
    monkeypatch.setattr(
        instance_control.instance_identity,
        "signal_verified_instance",
        lambda _record, sig, **_kwargs: signals.append(sig)
        or instance_identity.VerificationResult(
            True, "signal_sent", True, 4242, "instance-1", True
        ),
    )
    monkeypatch.setattr(
        instance_control.process_control,
        "clear_pid_record",
        lambda *_args, **_kwargs: cleared.append(True) or True,
    )

    result = instance_control.stop(
        tmp_path / "daemon.pid",
        database_dir=tmp_path,
        service_names=("heartbeat",),
        project_root=tmp_path,
        sleep=lambda _seconds: None,
    )

    assert result.exit_code == 0
    assert signals == [signal.SIGTERM]
    assert cleared == [True]


def test_stop_keeps_record_when_post_term_identity_is_temporarily_unverifiable(
    monkeypatch, tmp_path
):
    record = _record()
    checks = iter(
        [_result(True, "verified"), _result(False, "process_not_found_or_unverifiable")]
    )
    signals = []
    cleared = []
    monkeypatch.setattr(instance_control.process_control, "read_pid_record", lambda *_a, **_k: record)
    monkeypatch.setattr(instance_control, "_verify", lambda *_a, **_k: next(checks))
    monkeypatch.setattr(instance_control.platform, "system", lambda: "Linux")
    monkeypatch.setattr(instance_control.process_control, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        instance_control.instance_identity,
        "signal_verified_instance",
        lambda _record, sig, **_kwargs: signals.append(sig)
        or instance_identity.VerificationResult(
            True, "signal_sent", True, 4242, "instance-1", True
        ),
    )
    monkeypatch.setattr(
        instance_control.process_control,
        "clear_pid_record",
        lambda *_args, **_kwargs: cleared.append(True) or True,
    )

    result = instance_control.stop(
        tmp_path / "daemon.pid",
        database_dir=tmp_path,
        service_names=("heartbeat",),
        project_root=tmp_path,
        sleep=lambda _seconds: None,
    )

    assert result.exit_code == 1
    assert signals == [signal.SIGTERM]
    assert cleared == []
    assert "不发送 SIGKILL" in result.messages[0]
