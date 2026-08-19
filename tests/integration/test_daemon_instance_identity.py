# -*- coding: utf-8 -*-
"""Black-box safety probe for PID reuse protection."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from daemon import instance_identity


def test_reused_pid_never_signals_unrelated_live_process(tmp_path):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        observed = None
        for _ in range(20):
            observed = instance_identity.inspect_process(child.pid)
            if observed is not None:
                break
            time.sleep(0.05)
        if observed is None:
            pytest.skip("OS process fingerprint is unavailable on this runner")

        record = instance_identity.create_instance_record(
            database_dir=tmp_path,
            service_names=("heartbeat",),
            project_root=tmp_path,
            process_fingerprint=observed,
            instance_id="stale-daemon-instance",
            build_commit="test-build",
        )
        record["pid_start_time"] = "definitely-not-the-child-start-token"

        result = instance_identity.signal_verified_instance(
            record,
            15,
            database_dir=tmp_path,
            service_names=("heartbeat",),
            project_root=tmp_path,
        )

        assert result.ok is False
        assert result.reason == "pid_start_time_mismatch"
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)
