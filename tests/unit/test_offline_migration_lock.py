from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.ops.exclusive_file_lock import ExclusiveFileLockError
from core.ops.offline_migration_lock import offline_migration_lock


def test_mcp_writer_lifetime_blocks_offline_migration(tmp_path, monkeypatch):
    import core.config
    import integrations.agora as agora

    invoked = {"run": False}

    class FakeServer:
        def run(self) -> None:
            invoked["run"] = True
            with pytest.raises(
                ExclusiveFileLockError,
                match="MCP writer started before migration lock",
            ):
                with offline_migration_lock(
                    tmp_path,
                    daemon_check=lambda _path: True,
                ):
                    raise AssertionError("migration must not enter")

    monkeypatch.setattr(
        core.config,
        "get_config",
        lambda: SimpleNamespace(database_dir=tmp_path),
    )
    monkeypatch.setattr(
        agora,
        "build_mcp_server_from_environment",
        lambda: FakeServer(),
    )

    agora.run_mcp_server()

    assert invoked["run"] is True


def test_offline_migration_lifetime_blocks_mcp_writer_start(tmp_path, monkeypatch):
    import core.config
    import integrations.agora as agora

    monkeypatch.setattr(
        core.config,
        "get_config",
        lambda: SimpleNamespace(database_dir=tmp_path),
    )
    monkeypatch.setattr(
        agora,
        "build_mcp_server_from_environment",
        lambda: (_ for _ in ()).throw(AssertionError("server must not be built")),
    )

    with offline_migration_lock(tmp_path, daemon_check=lambda _path: True):
        with pytest.raises(
            ExclusiveFileLockError,
            match="offline migration is active",
        ):
            agora.run_mcp_server()


def test_offline_migration_lock_is_reentrant_only_for_the_holding_thread(tmp_path):
    checks = {"count": 0}

    def daemon_check(_path):
        checks["count"] += 1
        return True

    with offline_migration_lock(tmp_path, daemon_check=daemon_check):
        with offline_migration_lock(
            tmp_path,
            daemon_check=lambda _path: (_ for _ in ()).throw(
                AssertionError("nested lock must reuse the proven outer lease")
            ),
        ):
            assert checks["count"] == 1

    with offline_migration_lock(tmp_path, daemon_check=daemon_check):
        assert checks["count"] == 2
