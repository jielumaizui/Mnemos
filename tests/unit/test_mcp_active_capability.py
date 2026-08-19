import tomllib
import json
import stat
import sqlite3
import threading
import time

import pytest

from core.access_policy import MCP_TOOL_POLICIES
from core.agent_kit.authorization import (
    AgentAuthorizationStore,
    InMemoryMCPLaunchCredentialStore,
)
from integrations.active import (
    crush_mcp_configured,
    json_mcp_configured,
    kiro_mcp_configured,
    mcp_server_spec,
    opencode_mcp_configured,
    openclaw_mcp_configured,
    upsert_codex_mcp_server,
    upsert_crush_mcp_server,
    upsert_kiro_mcp_server,
    upsert_json_mcp_server,
    upsert_opencode_config,
    upsert_openclaw_mcp_server,
)


def test_mcp_server_spec_passes_only_keyring_reference_in_environment():
    reference = "keyring:mnemos.mcp.launch/codex/public-capability-id"
    spec = mcp_server_spec(launch_reference=reference)

    assert spec["env"] == {
        "MNEMOS_MCP_LAUNCH_CAPABILITY_REF": reference
    }
    assert "MNEMOS_MCP_LAUNCH_CAPABILITY" not in spec["env"]
    assert reference not in " ".join(spec["args"])


def test_codex_mcp_install_uses_authorization_store_capability(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    store.set_mcp_principal_grant(
        "codex",
        capabilities=set(MCP_TOOL_POLICIES.values()),
        allowed_projects={"mnemos"},
        allowed_source_agents={"claude"},
    )
    config_path = tmp_path / "config.toml"
    secret_store = InMemoryMCPLaunchCredentialStore()

    assert upsert_codex_mcp_server(
        config_path,
        authorization_store=store,
        credential_store=secret_store,
    ) is True

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    reference = config["mcp_servers"]["mnemos"]["env"][
        "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"
    ]
    credential = secret_store.resolve(reference)
    principal = store.resolve_mcp_principal(credential)
    assert principal.agent == "codex"
    assert principal.capabilities == frozenset(MCP_TOOL_POLICIES.values())
    assert principal.allowed_projects == frozenset({"mnemos"})
    assert principal.allowed_source_agents == frozenset({"claude"})


def test_legacy_content_authorization_does_not_auto_grant_mcp_read(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    store.set_state("codex", "user_authorized")
    config_path = tmp_path / "config.toml"
    secret_store = InMemoryMCPLaunchCredentialStore()

    assert upsert_codex_mcp_server(
        config_path,
        authorization_store=store,
        credential_store=secret_store,
    ) is True

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    reference = config["mcp_servers"]["mnemos"]["env"][
        "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"
    ]
    credential = secret_store.resolve(reference)
    principal = store.resolve_mcp_principal(credential)
    assert principal.capabilities == frozenset({"public_metadata"})
    assert principal.allowed_projects == frozenset()
    assert principal.allowed_source_agents == frozenset()


def test_codex_capability_rotation_revokes_replaced_credential(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    store.set_mcp_principal_grant("codex", capabilities={"memory_read"})
    config_path = tmp_path / "config.toml"
    secret_store = InMemoryMCPLaunchCredentialStore()

    upsert_codex_mcp_server(
        config_path,
        authorization_store=store,
        credential_store=secret_store,
    )
    first = tomllib.loads(config_path.read_text())["mcp_servers"]["mnemos"]["env"][
        "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"
    ]
    first_credential = secret_store.resolve(first)
    upsert_codex_mcp_server(
        config_path,
        authorization_store=store,
        credential_store=secret_store,
    )
    second = tomllib.loads(config_path.read_text())["mcp_servers"]["mnemos"]["env"][
        "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"
    ]
    second_credential = secret_store.resolve(second)

    assert first != second
    assert secret_store.resolve(first) == ""
    assert store.resolve_mcp_principal(first_credential) is None
    assert store.resolve_mcp_principal(second_credential) is not None


def test_config_update_failure_keeps_old_credential_and_revokes_orphan(
    tmp_path,
    monkeypatch,
):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    store.set_mcp_principal_grant("codex", capabilities={"memory_read"})
    config_path = tmp_path / "config.toml"
    secret_store = InMemoryMCPLaunchCredentialStore()
    upsert_codex_mcp_server(
        config_path,
        authorization_store=store,
        credential_store=secret_store,
    )
    first = tomllib.loads(config_path.read_text())["mcp_servers"]["mnemos"]["env"][
        "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"
    ]
    first_credential = secret_store.resolve(first)

    def fail_replace(_source, _target):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr("integrations.active.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated atomic replace failure"):
        upsert_codex_mcp_server(
            config_path,
            authorization_store=store,
            credential_store=secret_store,
        )

    persisted = tomllib.loads(config_path.read_text())["mcp_servers"]["mnemos"]["env"][
        "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"
    ]
    assert persisted == first
    assert secret_store.resolve(first) == first_credential
    assert store.resolve_mcp_principal(first_credential) is not None
    with sqlite3.connect(store.db_path) as conn:
        active_count = conn.execute(
            "SELECT COUNT(*) FROM mcp_launch_capabilities WHERE state = 'active'"
        ).fetchone()[0]
    assert active_count == 1


def test_rotation_activation_failure_restores_previous_config(tmp_path, monkeypatch):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    store.set_mcp_principal_grant("codex", capabilities={"memory_read"})
    config_path = tmp_path / "config.toml"
    secret_store = InMemoryMCPLaunchCredentialStore()
    upsert_codex_mcp_server(
        config_path,
        authorization_store=store,
        credential_store=secret_store,
    )
    first_reference = tomllib.loads(config_path.read_text())["mcp_servers"][
        "mnemos"
    ]["env"]["MNEMOS_MCP_LAUNCH_CAPABILITY_REF"]
    first_credential = secret_store.resolve(first_reference)
    monkeypatch.setattr(
        store,
        "activate_mcp_capability_rotation",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(RuntimeError, match="rotation activation failed"):
        upsert_codex_mcp_server(
            config_path,
            authorization_store=store,
            credential_store=secret_store,
        )

    persisted_reference = tomllib.loads(config_path.read_text())["mcp_servers"][
        "mnemos"
    ]["env"]["MNEMOS_MCP_LAUNCH_CAPABILITY_REF"]
    assert persisted_reference == first_reference
    assert store.resolve_mcp_principal(first_credential) is not None
    with sqlite3.connect(store.db_path) as conn:
        active_count = conn.execute(
            "SELECT COUNT(*) FROM mcp_launch_capabilities WHERE state = 'active'"
        ).fetchone()[0]
        prepared_count = conn.execute(
            "SELECT COUNT(*) FROM mcp_launch_capabilities WHERE state = 'prepared'"
        ).fetchone()[0]
    assert active_count == 1
    assert prepared_count == 0


def test_install_migrates_plaintext_launch_secret_to_keyring_reference(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    store.set_mcp_principal_grant("codex", capabilities={"memory_read"})
    legacy_credential = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[mcp_servers.mnemos]\n"
        'command = "python3"\n'
        'args = ["mnemos_cli.py", "mcp", "serve"]\n'
        f'env = {{ MNEMOS_MCP_LAUNCH_CAPABILITY = "{legacy_credential}" }}\n',
        encoding="utf-8",
    )
    backup_path = config_path.with_name(config_path.name + ".mnemos.bak")
    backup_path.write_text(
        f"legacy backup {legacy_credential}\n",
        encoding="utf-8",
    )
    secret_store = InMemoryMCPLaunchCredentialStore()

    assert upsert_codex_mcp_server(
        config_path,
        authorization_store=store,
        credential_store=secret_store,
    ) is True

    text = config_path.read_text(encoding="utf-8")
    config = tomllib.loads(text)
    env = config["mcp_servers"]["mnemos"]["env"]
    reference = env["MNEMOS_MCP_LAUNCH_CAPABILITY_REF"]
    assert "MNEMOS_MCP_LAUNCH_CAPABILITY =" not in text
    assert legacy_credential not in text
    backup_text = backup_path.read_text(encoding="utf-8")
    assert legacy_credential not in backup_text
    assert "<removed-during-mcp-migration>" in backup_text
    assert store.resolve_mcp_principal(legacy_credential) is None
    assert store.resolve_mcp_principal(secret_store.resolve(reference)) is not None


def test_revoked_grant_cannot_be_reinstalled_with_base_capability(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    store.set_mcp_principal_grant(
        "codex",
        capabilities={"memory_read"},
        state="revoked",
    )
    config_path = tmp_path / "config.toml"

    with pytest.raises(PermissionError, match="grant revoked"):
        upsert_codex_mcp_server(
            config_path,
            authorization_store=store,
            credential_store=InMemoryMCPLaunchCredentialStore(),
        )

    assert config_path.exists() is False


def test_prepared_capability_is_inactive_until_atomic_rotation(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    previous = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    replacement = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
        state="prepared",
    )

    assert store.resolve_mcp_principal(replacement) is None
    assert store.activate_mcp_capability_rotation(
        replacement,
        previous_capability_id=previous.partition(".")[0],
    ) is True
    assert store.resolve_mcp_principal(previous) is None
    assert store.resolve_mcp_principal(replacement) is not None


def test_rotation_activation_failure_rolls_back_predecessor_revocation(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "agent_authorization.db"
    store = AgentAuthorizationStore(db_path)
    previous = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    replacement = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
        state="prepared",
    )

    class FailingActivationConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            normalized = " ".join(str(sql).split()).lower()
            if (
                "update mcp_launch_capabilities" in normalized
                and "set state = 'active'" in normalized
            ):
                return super().execute(
                    "UPDATE mcp_launch_capabilities SET state='active' WHERE 0"
                )
            return super().execute(sql, parameters)

    def failing_connect():
        connection = sqlite3.connect(
            str(db_path),
            timeout=5,
            factory=FailingActivationConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    monkeypatch.setattr(store, "_connect", failing_connect)

    assert store.activate_mcp_capability_rotation(
        replacement,
        previous_capability_id=previous.partition(".")[0],
    ) is False
    assert store.resolve_mcp_principal(previous) is not None
    assert store.resolve_mcp_principal(replacement) is None
    with sqlite3.connect(db_path) as connection:
        states = dict(
            connection.execute(
                "SELECT capability_id, state FROM mcp_launch_capabilities"
            ).fetchall()
        )
    assert states[previous.partition(".")[0]] == "active"
    assert states[replacement.partition(".")[0]] == "prepared"


def test_rotation_recovers_from_abandoned_prepared_config_without_dual_active(tmp_path):
    store = AgentAuthorizationStore(tmp_path / "agent_authorization.db")
    orphaned_active = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    abandoned_prepared = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
        state="prepared",
    )
    replacement = store.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
        state="prepared",
    )

    assert store.activate_mcp_capability_rotation(
        replacement,
        previous_capability_id=abandoned_prepared.partition(".")[0],
    ) is True
    assert store.resolve_mcp_principal(orphaned_active) is None
    assert store.resolve_mcp_principal(abandoned_prepared) is None
    assert store.resolve_mcp_principal(replacement) is not None
    with sqlite3.connect(store.db_path) as conn:
        active_count = conn.execute(
            "SELECT COUNT(*) FROM mcp_launch_capabilities WHERE state = 'active'"
        ).fetchone()[0]
    assert active_count == 1


def test_concurrent_prepared_rotations_leave_exactly_one_active_capability(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "agent_authorization.db"
    setup = AgentAuthorizationStore(db_path)
    setup.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
    )
    first_credential = setup.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
        state="prepared",
    )
    second_credential = setup.issue_mcp_capability(
        agent="codex",
        host_kind="codex",
        capabilities={"memory_read"},
        state="prepared",
    )
    first_selected = threading.Event()
    release_first = threading.Event()

    class PausingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            normalized = " ".join(str(sql).split()).lower()
            if (
                "select secret_hash, agent, host_kind"
                in normalized
                and "state = 'prepared'" in normalized
                and not first_selected.is_set()
            ):
                first_selected.set()
                assert release_first.wait(timeout=5)
            return result

    first_store = AgentAuthorizationStore(db_path)
    second_store = AgentAuthorizationStore(db_path)

    def pausing_connect():
        conn = sqlite3.connect(
            str(db_path),
            timeout=5,
            factory=PausingConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    monkeypatch.setattr(first_store, "_connect", pausing_connect)
    results: list[bool] = []
    errors: list[BaseException] = []

    def rotate(store, credential):
        try:
            results.append(store.activate_mcp_capability_rotation(credential))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=rotate, args=(first_store, first_credential))
    second = threading.Thread(target=rotate, args=(second_store, second_credential))
    first.start()
    assert first_selected.wait(timeout=5)
    second.start()
    time.sleep(0.1)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert sorted(results) == [False, True]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM mcp_launch_capabilities WHERE state='active'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("agent", "writer", "reader", "credential_path", "configured"),
    [
        (
            "claude",
            lambda path, store, secrets: upsert_json_mcp_server(
                path,
                claude=True,
                agent="claude",
                authorization_store=store,
                credential_store=secrets,
            ),
            lambda path: json.loads(path.read_text()),
            ("mcpServers", "mnemos", "env", "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"),
            lambda path, store, secrets: json_mcp_configured(
                path, authorization_store=store, credential_store=secrets
            ),
        ),
        (
            "kimi",
            lambda path, store, secrets: upsert_json_mcp_server(
                path,
                agent="kimi",
                authorization_store=store,
                credential_store=secrets,
            ),
            lambda path: json.loads(path.read_text()),
            ("mcpServers", "mnemos", "env", "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"),
            lambda path, store, secrets: json_mcp_configured(
                path, authorization_store=store, credential_store=secrets
            ),
        ),
        (
            "crush",
            lambda path, store, secrets: upsert_crush_mcp_server(
                path,
                authorization_store=store,
                credential_store=secrets,
            ),
            lambda path: json.loads(path.read_text()),
            ("mcp", "mnemos", "env", "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"),
            lambda path, store, secrets: crush_mcp_configured(
                path, authorization_store=store, credential_store=secrets
            ),
        ),
        (
            "opencode",
            lambda path, store, secrets: upsert_opencode_config(
                path,
                authorization_store=store,
                credential_store=secrets,
            ),
            lambda path: json.loads(path.read_text()),
            ("mcp", "mnemos", "environment", "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"),
            lambda path, store, secrets: opencode_mcp_configured(
                path,
                authorization_store=store,
                credential_store=secrets,
            ),
        ),
        (
            "openclaw",
            lambda path, store, secrets: upsert_openclaw_mcp_server(
                path,
                authorization_store=store,
                credential_store=secrets,
            ),
            lambda path: json.loads(path.read_text()),
            ("mcp", "servers", "mnemos", "env", "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"),
            lambda path, store, secrets: openclaw_mcp_configured(
                path,
                authorization_store=store,
                credential_store=secrets,
            ),
        ),
        (
            "kiro",
            lambda path, store, secrets: upsert_kiro_mcp_server(
                path,
                authorization_store=store,
                credential_store=secrets,
            ),
            lambda path: json.loads(path.read_text()),
            ("mcpServers", "mnemos", "env", "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"),
            lambda path, store, secrets: kiro_mcp_configured(
                path,
                authorization_store=store,
                credential_store=secrets,
            ),
        ),
    ],
)
def test_each_host_install_uses_unique_server_capability(
    tmp_path,
    agent,
    writer,
    reader,
    credential_path,
    configured,
):
    store = AgentAuthorizationStore(tmp_path / f"{agent}.db")
    store.set_mcp_principal_grant(
        agent,
        capabilities=set(MCP_TOOL_POLICIES.values()),
    )
    path = tmp_path / f"{agent}.config"
    secret_store = InMemoryMCPLaunchCredentialStore()

    assert writer(path, store, secret_store) is True

    value = reader(path)
    for key in credential_path:
        value = value[key]
    principal = store.resolve_mcp_principal(secret_store.resolve(value))
    assert principal.agent == agent
    assert configured(path, store, secret_store) is True
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_configured_checks_reject_unsigned_mcp_entry(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"mnemos": mcp_server_spec()}}),
        encoding="utf-8",
    )

    assert json_mcp_configured(path) is False
