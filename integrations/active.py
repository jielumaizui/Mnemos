"""Shared helpers for Mnemos active agent integration.

This module owns the small amount of cross-agent glue needed to expose the
current Mnemos MCP server and to create a preflight context that host agents can
consume at session start. Passive capture still remains the fidelity fallback.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from core.agent_kit.authorization import (
    AgentAuthorizationStore,
    MCPLaunchCredentialStore,
)
from core.config import get_config
from integrations.mcp_config_security import (
    commit_rotated_config as _commit_rotated_mcp_config,
    reference_resolves as _resolve_mcp_reference,
)
from integrations.preflight_builder import build_lightweight_preflight

SERVER_NAME = "mnemos"
POLICY_MARKER = "MNEMOS_ACTIVE_POLICY"
_BASE_MCP_CAPABILITIES = frozenset({"public_metadata"})
_MCP_LAUNCH_REF_ENV = "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"
_LEGACY_MCP_LAUNCH_ENV = "MNEMOS_MCP_LAUNCH_CAPABILITY"
KIRO_MCP_TIMEOUT_MS = 60_000


def _issue_mcp_launch_credential(
    agent: str,
    authorization_store: AgentAuthorizationStore | None = None,
) -> str:
    """Issue least-privilege launch credentials from server-side grant state."""
    normalized_agent = str(agent).strip().lower()
    store = authorization_store or AgentAuthorizationStore()
    grant = store.get_mcp_principal_grant(normalized_agent)
    capabilities = set(_BASE_MCP_CAPABILITIES)
    allowed_projects: set[str] = set()
    allowed_source_agents: set[str] = set()
    if grant is not None and grant.state == "revoked":
        raise PermissionError(f"MCP principal grant revoked: {normalized_agent}")
    if grant is not None and grant.state == "active":
        capabilities = set(grant.capabilities)
        allowed_projects = set(grant.allowed_projects)
        allowed_source_agents = set(grant.allowed_source_agents)
    return store.issue_mcp_capability(
        agent=normalized_agent,
        host_kind=normalized_agent,
        capabilities=capabilities,
        allowed_projects=allowed_projects,
        allowed_source_agents=allowed_source_agents,
        state="prepared",
    )


def _launch_env(reference: str) -> Dict[str, str]:
    return {_MCP_LAUNCH_REF_ENV: reference} if reference else {}


def _spec_has_launch_credential(
    spec: Dict[str, Any],
    *,
    env_key: str = "env",
) -> bool:
    env = spec.get(env_key)
    return isinstance(env, dict) and bool(str(env.get(_MCP_LAUNCH_REF_ENV) or ""))


def _reference_from_spec(
    spec: Any,
    *,
    env_key: str = "env",
) -> str:
    if not isinstance(spec, dict):
        return ""
    env = spec.get(env_key)
    if not isinstance(env, dict):
        return ""
    return str(env.get(_MCP_LAUNCH_REF_ENV) or "")


def _plaintext_credential_from_spec_for_migration(
    spec: Any,
    *,
    env_key: str = "env",
) -> str:
    """Read only the old plaintext field so installers can remove it safely."""
    if not isinstance(spec, dict):
        return ""
    env = spec.get(env_key)
    if not isinstance(env, dict):
        return ""
    return str(env.get(_LEGACY_MCP_LAUNCH_ENV) or "")


def _prepare_mcp_launch(
    agent: str,
    store: AgentAuthorizationStore,
    credential_store: MCPLaunchCredentialStore,
) -> tuple[str, str]:
    """Issue a capability, persist the secret, and return reference plus secret."""
    credential = _issue_mcp_launch_credential(agent, store)
    try:
        reference = credential_store.store(agent, credential)
    except (ImportError, OSError, RuntimeError, ValueError):
        store.revoke_mcp_capability(credential)
        raise
    return reference, credential


def _rotation_material(
    spec: Any,
    *,
    agent: str,
    store: AgentAuthorizationStore,
    credential_store: MCPLaunchCredentialStore,
    env_key: str = "env",
) -> tuple[str, str, str, str]:
    """Collect old launch state and prepare a replacement reference."""
    previous_reference = _reference_from_spec(spec, env_key=env_key)
    previous_credential = _plaintext_credential_from_spec_for_migration(
        spec,
        env_key=env_key,
    )
    if previous_reference and not previous_credential:
        try:
            previous_credential = credential_store.resolve(previous_reference)
        except (ImportError, OSError, RuntimeError, ValueError):
            previous_credential = ""
    new_reference, new_credential = _prepare_mcp_launch(
        agent,
        store,
        credential_store,
    )
    return (
        previous_reference,
        previous_credential,
        new_reference,
        new_credential,
    )


def _reference_resolves(
    reference: str,
    authorization_store: AgentAuthorizationStore | None,
    credential_store: MCPLaunchCredentialStore | None,
) -> bool:
    """Resolve through injectable stores used by host installers and tests."""
    return _resolve_mcp_reference(
        reference,
        authorization_store or AgentAuthorizationStore(initialize=False),
        credential_store or MCPLaunchCredentialStore(),
    )


def mnemos_root() -> Path:
    return Path(__file__).resolve().parents[1]


def mnemos_cli_path() -> Path:
    return mnemos_root() / "mnemos_cli.py"


def mcp_server_spec(
    python_cmd: str | None = None,
    *,
    claude: bool = False,
    launch_reference: str = "",
) -> Dict[str, Any]:
    """Return the stdio MCP server spec used by supported agents."""
    spec: Dict[str, Any] = {
        "command": python_cmd or sys.executable,
        "args": [mnemos_cli_path().as_posix(), "mcp", "serve"],
    }
    env = _launch_env(launch_reference)
    if claude:
        spec = {"type": "stdio", **spec, "env": env}
    elif env:
        spec["env"] = env
    return spec


def crush_mcp_server_spec(
    python_cmd: str | None = None,
    *,
    launch_reference: str = "",
) -> Dict[str, Any]:
    """Return the Crush-flavored stdio MCP server spec.

    Crush stores MCP servers under the ``mcp`` top-level key and expects each
    entry to declare its transport ``type`` (stdio/http/sse).
    """
    return {
        "type": "stdio",
        "command": python_cmd or sys.executable,
        "args": [mnemos_cli_path().as_posix(), "mcp", "serve"],
        "timeout": 120,
        "env": _launch_env(launch_reference),
    }


def crush_config_path() -> Path:
    return Path.home() / ".config" / "crush" / "crush.json"


def kiro_mcp_config_path(home: Path | None = None) -> Path:
    """Return the global Kiro CLI MCP registry path.

    Kiro CLI reads its global MCP registrations from ``settings/mcp.json``;
    the older ``~/.kiro/config.yaml`` is not consulted by the active CLI.
    """
    return (home or Path.home()) / ".kiro" / "settings" / "mcp.json"


def kiro_mcp_server_spec(
    python_cmd: str | None = None,
    *,
    launch_reference: str = "",
) -> Dict[str, Any]:
    """Return the Kiro CLI stdio server spec.

    Kiro interprets ``timeout`` as milliseconds.  A cold Mnemos health
    handshake may legitimately take longer than its short default.
    """
    spec = mcp_server_spec(
        python_cmd,
        launch_reference=launch_reference,
    )
    spec["timeout"] = KIRO_MCP_TIMEOUT_MS
    return spec


def opencode_mcp_server_spec(
    python_cmd: str | None = None,
    *,
    launch_reference: str = "",
) -> Dict[str, Any]:
    """Return the current OpenCode local MCP server spec."""
    return {
        "type": "local",
        "command": [
            python_cmd or sys.executable,
            mnemos_cli_path().as_posix(),
            "mcp",
            "serve",
        ],
        "enabled": True,
        # The Mnemos health handshake may include a cold local import and its
        # read-only storage checks.  OpenCode treats this value as milliseconds;
        # ten seconds expires before a healthy cold handshake can complete.
        "timeout": 60000,
        "environment": _launch_env(launch_reference),
    }


def codex_mcp_table(
    python_cmd: str | None = None,
    *,
    launch_reference: str = "",
) -> str:
    spec = mcp_server_spec(python_cmd, launch_reference=launch_reference)
    args = ", ".join(json.dumps(a, ensure_ascii=False) for a in spec["args"])
    table = (
        f"[mcp_servers.{SERVER_NAME}]\n"
        f"command = {json.dumps(spec['command'], ensure_ascii=False)}\n"
        f"args = [{args}]\n"
    )
    if launch_reference:
        encoded = json.dumps(launch_reference, ensure_ascii=False)
        table += f"env = {{ MNEMOS_MCP_LAUNCH_CAPABILITY_REF = {encoded} }}\n"
    return table


def write_text_if_changed(path: Path, text: str, *, backup: bool = True) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return True
    if backup and old is not None:
        backup_path = path.with_name(path.name + ".mnemos.bak")
        if not backup_path.exists():
            backup_path.write_text(old, encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    return True


def load_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _strip_jsonc_comments(text: str) -> str:
    """Best-effort JSONC cleaner for user config files.

    OpenCode accepts JSONC. Python's stdlib does not, so we preserve data by
    stripping comments/trailing commas and writing a clean JSON file with a
    backup when comments were present.
    """
    out = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    cleaned = "".join(out)
    return re.sub(r",\s*([}\]])", r"\1", cleaned)


def load_jsonc_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        try:
            data = json.loads(_strip_jsonc_comments(raw))
        except (json.JSONDecodeError, ValueError):
            return {}
    return data if isinstance(data, dict) else {}


def upsert_json_mcp_server(
    path: Path,
    *,
    top_key: str = "mcpServers",
    claude: bool = False,
    agent: str = "kimi",
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
    server_spec_builder: Callable[..., Dict[str, Any]] | None = None,
) -> bool:
    data = load_json_file(path)
    servers = data.get(top_key)
    if not isinstance(servers, dict):
        servers = {}
        data[top_key] = servers
    store = authorization_store or AgentAuthorizationStore()
    secret_store = credential_store or MCPLaunchCredentialStore()
    previous_reference, previous_credential, new_reference, new_credential = (
        _rotation_material(
            servers.get(SERVER_NAME),
            agent=agent,
            store=store,
            credential_store=secret_store,
        )
    )
    if server_spec_builder is None:
        servers[SERVER_NAME] = mcp_server_spec(
            claude=claude,
            launch_reference=new_reference,
        )
    else:
        servers[SERVER_NAME] = server_spec_builder(launch_reference=new_reference)
    return _commit_rotated_mcp_config(
        path,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        store=store,
        credential_store=secret_store,
        previous_reference=previous_reference,
        previous_credential=previous_credential,
        new_reference=new_reference,
        new_credential=new_credential,
    )


def json_mcp_configured(
    path: Path,
    *,
    top_key: str = "mcpServers",
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    data = load_json_file(path)
    servers = data.get(top_key)
    if not isinstance(servers, dict):
        return False
    spec = servers.get(SERVER_NAME)
    if not isinstance(spec, dict):
        return False
    args = spec.get("args") or []
    reference = _reference_from_spec(spec)
    return bool(
        spec.get("command")
        and mnemos_cli_path().as_posix() in [str(a) for a in args]
        and _spec_has_launch_credential(spec)
        and _reference_resolves(reference, authorization_store, credential_store)
    )


def upsert_kiro_mcp_server(
    path: Path,
    *,
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    """Install Mnemos into the actual Kiro CLI global MCP registry."""
    return upsert_json_mcp_server(
        path,
        agent="kiro",
        authorization_store=authorization_store,
        credential_store=credential_store,
        server_spec_builder=kiro_mcp_server_spec,
    )


def kiro_mcp_configured(
    path: Path,
    *,
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    """Check the exact Kiro CLI MCP contract, including its timeout unit."""
    if not json_mcp_configured(
        path,
        authorization_store=authorization_store,
        credential_store=credential_store,
    ):
        return False
    data = load_json_file(path)
    spec = data.get("mcpServers", {}).get(SERVER_NAME)
    return isinstance(spec, dict) and spec.get("timeout") == KIRO_MCP_TIMEOUT_MS


def upsert_crush_mcp_server(
    path: Path,
    *,
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    """Install or update the Mnemos MCP entry in Crush's ``crush.json``.

    Crush uses a top-level ``mcp`` object where each key is a named server.
    The file is created if it does not exist. Existing unrelated keys are
    preserved.
    """
    data = load_json_file(path)
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        mcp = {}
        data["mcp"] = mcp
    store = authorization_store or AgentAuthorizationStore()
    secret_store = credential_store or MCPLaunchCredentialStore()
    previous_reference, previous_credential, new_reference, new_credential = (
        _rotation_material(
            mcp.get(SERVER_NAME),
            agent="crush",
            store=store,
            credential_store=secret_store,
        )
    )
    mcp[SERVER_NAME] = crush_mcp_server_spec(
        launch_reference=new_reference,
    )
    # Preserve Crush's schema marker when creating a fresh file.
    if "$schema" not in data:
        data["$schema"] = "https://charm.land/crush.json"
    return _commit_rotated_mcp_config(
        path,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        store=store,
        credential_store=secret_store,
        previous_reference=previous_reference,
        previous_credential=previous_credential,
        new_reference=new_reference,
        new_credential=new_credential,
    )


def crush_mcp_configured(
    path: Path,
    *,
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    data = load_json_file(path)
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return False
    spec = mcp.get(SERVER_NAME)
    if not isinstance(spec, dict):
        return False
    if spec.get("type") != "stdio":
        return False
    args = spec.get("args") or []
    reference = _reference_from_spec(spec)
    return bool(
        spec.get("command")
        and mnemos_cli_path().as_posix() in [str(a) for a in args]
        and _spec_has_launch_credential(spec)
        and _reference_resolves(reference, authorization_store, credential_store)
    )


def upsert_opencode_config(
    path: Path,
    *,
    include_mcp: bool = True,
    include_policy: bool = True,
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    """Write current OpenCode config fields while preserving unrelated keys."""
    data = load_jsonc_file(path)
    if include_mcp:
        mcp = data.get("mcp")
        if not isinstance(mcp, dict):
            mcp = {}
            data["mcp"] = mcp
        store = authorization_store or AgentAuthorizationStore()
        secret_store = credential_store or MCPLaunchCredentialStore()
        previous_reference, previous_credential, new_reference, new_credential = (
            _rotation_material(
                mcp.get(SERVER_NAME),
                agent="opencode",
                store=store,
                credential_store=secret_store,
                env_key="environment",
            )
        )
        mcp[SERVER_NAME] = opencode_mcp_server_spec(
            launch_reference=new_reference,
        )
    if include_policy:
        policy = str(write_active_policy_file())
        instructions = data.get("instructions")
        if isinstance(instructions, str):
            instructions = [instructions]
        elif not isinstance(instructions, list):
            instructions = []
        cleaned_instructions: List[Any] = []
        for item in instructions:
            item_text = str(item)
            if (
                item_text != policy
                and "MNEMOS_ACTIVE" in item_text.upper()
                and not Path(item_text).expanduser().exists()
            ):
                continue
            cleaned_instructions.append(item)
        instructions = cleaned_instructions
        if policy not in [str(x) for x in instructions]:
            instructions.append(policy)
        data["instructions"] = instructions
    if not include_mcp:
        return write_text_if_changed(
            path,
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        )
    return _commit_rotated_mcp_config(
        path,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        store=store,
        credential_store=secret_store,
        previous_reference=previous_reference,
        previous_credential=previous_credential,
        new_reference=new_reference,
        new_credential=new_credential,
    )


def opencode_mcp_configured(
    path: Path,
    *,
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    data = load_jsonc_file(path)
    spec = data.get("mcp", {}).get(SERVER_NAME)
    if not isinstance(spec, dict):
        return False
    command = spec.get("command") or []
    if isinstance(command, str):
        command = [command]
    reference = _reference_from_spec(spec, env_key="environment")
    return (
        mnemos_cli_path().as_posix() in [str(a) for a in command]
        and _spec_has_launch_credential(spec, env_key="environment")
        and _reference_resolves(reference, authorization_store, credential_store)
    )


def opencode_policy_configured(path: Path) -> bool:
    data = load_jsonc_file(path)
    instructions = data.get("instructions")
    if isinstance(instructions, str):
        instructions = [instructions]
    if not isinstance(instructions, list):
        return False
    return str(active_policy_path()) in [str(x) for x in instructions]


def upsert_openclaw_mcp_server(
    path: Path,
    *,
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    data = load_json_file(path)
    mcp = data.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        mcp = {}
        data["mcp"] = mcp
    servers = mcp.setdefault("servers", {})
    if not isinstance(servers, dict):
        servers = {}
        mcp["servers"] = servers
    store = authorization_store or AgentAuthorizationStore()
    secret_store = credential_store or MCPLaunchCredentialStore()
    previous_reference, previous_credential, new_reference, new_credential = (
        _rotation_material(
            servers.get(SERVER_NAME),
            agent="openclaw",
            store=store,
            credential_store=secret_store,
        )
    )
    servers[SERVER_NAME] = mcp_server_spec(
        launch_reference=new_reference,
    )
    return _commit_rotated_mcp_config(
        path,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        store=store,
        credential_store=secret_store,
        previous_reference=previous_reference,
        previous_credential=previous_credential,
        new_reference=new_reference,
        new_credential=new_credential,
    )


def openclaw_mcp_configured(
    path: Path,
    *,
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    data = load_json_file(path)
    try:
        spec = data["mcp"]["servers"][SERVER_NAME]
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
    ):
        return False
    args = spec.get("args") or []
    reference = _reference_from_spec(spec)
    return bool(
        spec.get("command")
        and mnemos_cli_path().as_posix() in [str(a) for a in args]
        and _spec_has_launch_credential(spec)
        and _reference_resolves(reference, authorization_store, credential_store)
    )


def upsert_codex_mcp_server(
    path: Path,
    *,
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        existing_spec = tomllib.loads(old).get("mcp_servers", {}).get(SERVER_NAME, {})
    except (OSError, ValueError, TypeError):
        existing_spec = {}
    store = authorization_store or AgentAuthorizationStore()
    secret_store = credential_store or MCPLaunchCredentialStore()
    previous_reference, previous_credential, new_reference, new_credential = (
        _rotation_material(
            existing_spec,
            agent="codex",
            store=store,
            credential_store=secret_store,
        )
    )
    pattern = re.compile(r"(?ms)^\[mcp_servers\.mnemos\]\n.*?(?=^\[|\Z)")
    stripped = pattern.sub("", old).rstrip()
    table = codex_mcp_table(
        launch_reference=new_reference,
    )
    new = (stripped + "\n\n" if stripped else "") + table
    return _commit_rotated_mcp_config(
        path,
        new,
        store=store,
        credential_store=secret_store,
        previous_reference=previous_reference,
        previous_credential=previous_credential,
        new_reference=new_reference,
        new_credential=new_credential,
    )


def codex_mcp_configured(
    path: Path,
    *,
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    try:
        spec = tomllib.loads(text).get("mcp_servers", {}).get(SERVER_NAME, {})
    except (OSError, ValueError, TypeError):
        return False
    args = spec.get("args") or [] if isinstance(spec, dict) else []
    reference = _reference_from_spec(spec)
    return bool(
        isinstance(spec, dict)
        and spec.get("command")
        and mnemos_cli_path().as_posix() in [str(arg) for arg in args]
        and _spec_has_launch_credential(spec)
        and _reference_resolves(reference, authorization_store, credential_store)
    )


def upsert_marked_block(path: Path, content: str, *, marker: str = POLICY_MARKER) -> bool:
    start = f"<!-- BEGIN {marker} -->"
    end = f"<!-- END {marker} -->"
    block = f"{start}\n{content.rstrip()}\n{end}"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(
        rf"(?ms)<!-- BEGIN {re.escape(marker)} -->.*?<!-- END {re.escape(marker)} -->"
    )
    if pattern.search(old):
        new = pattern.sub(block, old)
    else:
        new = (old.rstrip() + "\n\n" if old.strip() else "") + block + "\n"
    return write_text_if_changed(path, new)


def marked_block_installed(path: Path, *, marker: str = POLICY_MARKER) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return f"<!-- BEGIN {marker} -->" in text and f"<!-- END {marker} -->" in text


def upsert_yaml_mcp_server(
    path: Path,
    *,
    top_key: str = "mcp_servers",
    agent: str,
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    try:
        import yaml
    except ImportError:
        return False
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            data = {}
    else:
        data = {}
    servers = data.get(top_key)
    if not isinstance(servers, dict):
        servers = {}
        data[top_key] = servers
    store = authorization_store or AgentAuthorizationStore()
    secret_store = credential_store or MCPLaunchCredentialStore()
    previous_reference, previous_credential, new_reference, new_credential = (
        _rotation_material(
            servers.get(SERVER_NAME),
            agent=agent,
            store=store,
            credential_store=secret_store,
        )
    )
    servers[SERVER_NAME] = mcp_server_spec(
        launch_reference=new_reference,
    )
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return _commit_rotated_mcp_config(
        path,
        text,
        store=store,
        credential_store=secret_store,
        previous_reference=previous_reference,
        previous_credential=previous_credential,
        new_reference=new_reference,
        new_credential=new_credential,
    )


def yaml_mcp_configured(
    path: Path,
    *,
    top_key: str = "mcp_servers",
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> bool:
    try:
        import yaml
    except ImportError:
        return False
    if not path.exists():
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        spec = data.get(top_key, {}).get(SERVER_NAME)
    except (OSError, IOError):
        return False
    if not isinstance(spec, dict):
        return False
    args = spec.get("args") or []
    reference = _reference_from_spec(spec)
    return bool(
        spec.get("command")
        and mnemos_cli_path().as_posix() in [str(a) for a in args]
        and _spec_has_launch_credential(spec)
        and _reference_resolves(reference, authorization_store, credential_store)
    )


def _strip_toml_strings(line: str) -> str:
    """Replace TOML string contents with spaces to avoid bracket counting inside strings."""
    result: List[str] = []
    in_str = False
    escape = False
    for ch in line:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            result.append(" ")
        else:
            if ch == '"':
                in_str = True
                result.append(" ")
            else:
                result.append(ch)
    return "".join(result)


def _parse_kimi_config(text: str) -> Optional[Dict[str, Any]]:
    """尝试解析 TOML 配置；解析失败时返回 None。"""
    if not text.strip():
        return None
    try:
        return cast(Dict[str, Any], tomllib.loads(text))
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        return None


def _collect_existing_hook_keys(lines: List[str]) -> set[tuple[str, str]]:
    """扫描已有的 ``[[hooks]]`` 块，收集 (event, command) 键。"""
    existing: set[tuple[str, str]] = set()
    i = 0
    while i < len(lines):
        if lines[i].strip() == "[[hooks]]":
            block_lines = [lines[i]]
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith("[") or nxt.startswith("[["):
                    break
                block_lines.append(lines[j])
                j += 1
            block = "\n".join(block_lines)
            event_match = re.search(r'event\s*=\s*"([^"]+)"', block)
            cmd_match = re.search(r'command\s*=\s*"([^"]+)"', block)
            if event_match and cmd_match:
                existing.add((event_match.group(1), cmd_match.group(1)))
            i = j
            continue
        i += 1
    return existing


def _extract_preserved_hooks(
    parsed: Optional[Dict[str, Any]],
    wrapper_str: str,
    existing_hook_keys: set[tuple[str, str]],
) -> List[Dict[str, Any]]:
    """从解析后的旧 ``hooks = [...]`` 数组中提取需要保留的非 Mnemos hooks。"""
    if parsed is None or "hooks" not in parsed:
        return []
    preserved: List[Dict[str, Any]] = []
    for h in parsed.get("hooks", []):
        if not isinstance(h, dict):
            continue
        event = h.get("event", "")
        cmd = h.get("command", "")
        if wrapper_str in cmd or "mnemos_wrapper.py" in cmd:
            continue
        if (event, cmd) in existing_hook_keys:
            continue
        preserved.append(h)
    return preserved


def _rewrite_config_lines(lines: List[str], parsed: Optional[Dict[str, Any]], wrapper_str: str) -> List[str]:
    """移除旧 ``hooks = [...]`` 数组和 Mnemos ``[[hooks]]`` 块，保留其余内容。"""
    out: List[str] = []
    if parsed is None:
        if lines:
            out.append("\n".join(lines).rstrip())
        return out

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if re.match(r"^\s*hooks\s*=\s*\[", line):
            stripped_line = _strip_toml_strings(line)
            depth = stripped_line.count("[") - stripped_line.count("]")
            i += 1
            while i < len(lines) and depth > 0:
                stripped_cur = _strip_toml_strings(lines[i])
                depth += stripped_cur.count("[") - stripped_cur.count("]")
                i += 1
            continue

        if stripped == "[[hooks]]":
            block_lines = [line]
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith("[") or nxt.startswith("[["):
                    break
                block_lines.append(lines[j])
                j += 1
            block = "\n".join(block_lines)
            if wrapper_str in block or "mnemos_wrapper.py" in block:
                i = j
                continue
            out.extend(block_lines)
            i = j
            continue

        out.append(line)
        i += 1

    while out and out[-1].strip() == "":
        out.pop()
    return out


def _append_preserved_hooks(out: List[str], preserved_hooks: List[Dict[str, Any]]) -> None:
    """把保留下来的 hooks 以 ``[[hooks]]`` 形式追加回去。"""
    for h in preserved_hooks:
        out.append("")
        out.append("[[hooks]]")
        if "event" in h:
            out.append(f"event = {json.dumps(h['event'], ensure_ascii=False)}")
        for k, v in h.items():
            if k == "event":
                continue
            if isinstance(v, str):
                out.append(f"{k} = {json.dumps(v, ensure_ascii=False)}")


def _kimi_hook_command(wrapper_str: str, event_arg: str, python_cmd: str | None = None) -> str:
    """Build the Kimi hook shell command using the installed interpreter."""
    parts = [python_cmd or sys.executable, wrapper_str, event_arg]
    if os.name == "nt":
        return subprocess.list2cmdline([str(part) for part in parts])
    return " ".join(shlex.quote(str(part)) for part in parts)


def _append_mnemos_hooks(out: List[str], wrapper_str: str, python_cmd: str | None = None) -> None:
    """追加 Mnemos 的 SessionStart / SessionEnd hooks。"""
    out.append("")
    out.append("[[hooks]]")
    out.append('event = "SessionStart"')
    out.append(
        "command = "
        + json.dumps(
            _kimi_hook_command(wrapper_str, "--session-start", python_cmd),
            ensure_ascii=False,
        )
    )
    out.append("")
    out.append("[[hooks]]")
    out.append('event = "SessionEnd"')
    out.append(
        "command = "
        + json.dumps(
            _kimi_hook_command(wrapper_str, "--session-end", python_cmd),
            ensure_ascii=False,
        )
    )


def upsert_kimi_hooks(config_path: Path, wrapper_path: Path, python_cmd: str | None = None) -> bool:
    """Install Kimi hook commands using the native ``[[hooks]]`` table-array format.

    Kimi stores hooks in the main TOML config as ``[[hooks]]`` entries, while MCP
    servers live in ``~/.kimi/mcp.json``. This writer removes the historical
    ``hooks = [...]`` array (which conflicts with ``[[hooks]]`` in some TOML
    parsers) and any existing Mnemos ``[[hooks]]`` blocks, preserves unrelated
    hooks, and re-adds the current Mnemos hooks as ``[[hooks]]`` entries.
    """
    wrapper_str = str(wrapper_path)
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = text.splitlines()

    parsed = _parse_kimi_config(text)
    existing_hook_keys = _collect_existing_hook_keys(lines)
    preserved_hooks = _extract_preserved_hooks(parsed, wrapper_str, existing_hook_keys)
    out = _rewrite_config_lines(lines, parsed, wrapper_str)

    _append_preserved_hooks(out, preserved_hooks)
    _append_mnemos_hooks(out, wrapper_str, python_cmd=python_cmd)

    final = "\n".join(out).rstrip() + "\n"
    return write_text_if_changed(config_path, final)


def kimi_hooks_configured(
    config_path: Path, wrapper_path: Path, python_cmd: str | None = None
) -> bool:
    if not config_path.exists() or not wrapper_path.exists():
        return False
    text = config_path.read_text(encoding="utf-8")
    if python_cmd:
        return (
            _kimi_hook_command(str(wrapper_path), "--session-start", python_cmd) in text
            and _kimi_hook_command(str(wrapper_path), "--session-end", python_cmd) in text
        )
    hooks = _iter_kimi_hooks(text)
    return _has_kimi_hook(hooks, "SessionStart", wrapper_path, "--session-start") and _has_kimi_hook(
        hooks, "SessionEnd", wrapper_path, "--session-end"
    )


def _iter_kimi_hooks(text: str) -> List[Dict[str, str]]:
    parsed = _parse_kimi_config(text)
    if parsed is not None:
        hooks = parsed.get("hooks", [])
        if isinstance(hooks, list):
            return [
                {
                    "event": str(h.get("event") or ""),
                    "command": str(h.get("command") or ""),
                }
                for h in hooks
                if isinstance(h, dict)
            ]
    return _scan_kimi_hook_blocks(text)


def _scan_kimi_hook_blocks(text: str) -> List[Dict[str, str]]:
    hooks: List[Dict[str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() != "[[hooks]]":
            i += 1
            continue
        block_lines = []
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if nxt.startswith("[") or nxt.startswith("[["):
                break
            block_lines.append(lines[i])
            i += 1
        block = "\n".join(block_lines)
        event_match = re.search(r'event\s*=\s*"([^"]+)"', block)
        command_match = re.search(r'command\s*=\s*"((?:\\.|[^"])*)"', block)
        hooks.append(
            {
                "event": event_match.group(1) if event_match else "",
                "command": _decode_toml_string(command_match.group(1)) if command_match else "",
            }
        )
    return hooks


def _decode_toml_string(value: str) -> str:
    try:
        decoded = json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value
    return str(decoded)


def _has_kimi_hook(
    hooks: List[Dict[str, str]],
    event: str,
    wrapper_path: Path,
    event_arg: str,
) -> bool:
    for hook in hooks:
        if hook.get("event") != event:
            continue
        if _kimi_hook_command_targets_wrapper(
            hook.get("command", ""),
            wrapper_path,
            event_arg,
        ):
            return True
    return False


def _kimi_hook_command_targets_wrapper(command: str, wrapper_path: Path, event_arg: str) -> bool:
    if not command:
        return False
    wrapper = str(wrapper_path)
    try:
        parts = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        parts = command.split()
    if event_arg not in parts:
        return False
    return any(_same_hook_path(part, wrapper) for part in parts)


def _same_hook_path(candidate: str, expected: str) -> bool:
    if candidate == expected:
        return True
    try:
        return Path(candidate).expanduser() == Path(expected).expanduser()
    except (OSError, ValueError):
        return False


def active_policy_path() -> Path:
    return get_config().data_dir / "active_policy" / "MNEMOS_ACTIVE.md"


def active_policy_text(agent: str = "") -> str:
    agent_line = f" for {agent}" if agent else ""
    return f"""# Mnemos Active Policy{agent_line}

You have access to Mnemos as the user's long-term AI memory and knowledge system.

Before meaningful planning or answering:
- Use Mnemos startup context if it is provided.
- For coding, debugging, architecture, review, writing, or project decisions, call the Mnemos MCP tool `preflight_inject` or the server-prefixed equivalent.  # noqa: E501
- If the task asks for raw quotes, evidence, chat logs, or prior conversation details, call `session_search`.  # noqa: E501
- If the task asks for durable knowledge, standards, lessons, decisions, or preferences, call `context_aware_search` or `wiki_search`.  # noqa: E501
- If the task asks "how did we solve this last time", call both `session_search` and `context_aware_search`.  # noqa: E501
- At session start, task wrap-up, or when the user mentions review/recap/follow-up, call `check_pending_recaps` and surface one concise recap nudge if it returns an urgent or force-open item.  # noqa: E501

During the task:
- Prefer specific retrieved evidence over vague memory.
- Keep Mnemos context concise; do not paste large dumps unless the user asks.
- When retrieved knowledge conflicts with the user's latest explicit instruction, explain the conflict and follow the latest instruction unless it is unsafe.  # noqa: E501
- If the same file/tool is inspected twice by default (configurable via `guard.analysis_loop.max_repeated_reads_per_target`), analysis grows long without edits, or the user clearly asked to fix/modify/commit while you are still analyzing, call `guard_check` with `context` containing recent `tool_calls`/`current_file`/`current_tool`, then switch to action mode unless it reports a blocking risk.  # noqa: E501
- When the user is starting a task that resembles known work, call `predictive_push` once with the current user input/working directory. If it returns a relevant push, mention it briefly as "Mnemos found..." and apply it; then ask the user if it was helpful and call `push_feedback(delivery_event_id=<delivery_event_id>, topic=<topic>, action=accept|ignore|dismiss)`. For inaccurate/outdated, first obtain the latest canonical feedback event plus an exact correction target and reason. If it returns nothing useful, stay quiet.  # noqa: E501
- When the user's intent is ambiguous or `intent_route` returns `needs_correction=true`, confirm the real intent with the user and call `intent_correct(user_input, original_intent, corrected_intent)` to record the correction.  # noqa: E501
- If Mnemos materially changes your plan, recommendation, or warning, tell the user in one short sentence. Do not spam status text when Mnemos did not add value.  # noqa: E501

Before finalizing high-impact output:
- Call `guard_check` when the answer changes code, architecture, deployment, data capture, security, user workflows, or system behavior.  # noqa: E501
- Re-check `check_pending_recaps` when the work created a decision, fix, or lesson that may need a follow-up.

At session end or after valuable decisions:
- Use `capture_session`, `capture_turn`, or `end_session` when available.
- Passive capture via Mnemos Source modules remains the fidelity fallback, but do not rely on it as the only path when an explicit Mnemos tool is available.  # noqa: E501

If a Mnemos MCP tool is unavailable:
- Say so briefly when it matters, then proceed with the local startup context and the user's latest instruction.  # noqa: E501
"""


def write_active_policy_file() -> Path:
    path = active_policy_path()
    write_text_if_changed(path, active_policy_text(), backup=False)
    return path


def _agent_policy_path(agent: str) -> Optional[Path]:
    """返回 Agent 政策文件路径；None 表示不通过 marked block 安装。"""
    from core.agent_kit.source_support_manifest import AgentSourceSupportManifestError
    from core.agent_kit.source_support_manifest import get_agent_source_support_manifest

    try:
        agent = get_agent_source_support_manifest().require_host_agent(agent).name
    except AgentSourceSupportManifestError:
        return None
    if agent == "claude":
        return get_config().claude_data_dir / "CLAUDE.md"
    if agent == "codex":
        return Path.home() / ".codex" / "AGENTS.md"
    if agent == "kimi":
        return Path.home() / ".kimi" / "MNEMOS_ACTIVE.md"
    if agent == "hermes":
        return Path.home() / ".hermes" / "MNEMOS_ACTIVE.md"
    if agent == "kiro":
        return Path.home() / ".kiro" / "MNEMOS_ACTIVE.md"
    if agent == "openclaw":
        return Path.home() / ".openclaw" / "MNEMOS_ACTIVE.md"
    if agent == "crush":
        return Path.home() / ".config" / "crush" / "CRUSH.md"
    return None


def install_agent_policy(agent: str) -> bool:
    from core.agent_kit.source_support_manifest import AgentSourceSupportManifestError
    from core.agent_kit.source_support_manifest import get_agent_source_support_manifest

    try:
        agent = get_agent_source_support_manifest().require_host_agent(agent).name
    except AgentSourceSupportManifestError:
        return False
    policy = write_active_policy_file()
    if agent == "opencode":
        return upsert_opencode_config(
            opencode_config_path(), include_mcp=False, include_policy=True
        )
    path = _agent_policy_path(agent)
    if path:
        return upsert_marked_block(path, active_policy_text(agent))
    return policy.exists()


def is_agent_policy_installed(agent: str) -> bool:
    from core.agent_kit.source_support_manifest import AgentSourceSupportManifestError
    from core.agent_kit.source_support_manifest import get_agent_source_support_manifest

    try:
        agent = get_agent_source_support_manifest().require_host_agent(agent).name
    except AgentSourceSupportManifestError:
        return False
    if not active_policy_path().exists():
        return False
    if agent == "opencode":
        return opencode_policy_configured(opencode_config_path())
    path = _agent_policy_path(agent)
    if path:
        return marked_block_installed(path)
    return True


def opencode_config_path() -> Path:
    return Path.home() / ".config" / "opencode" / "opencode.json"


def active_context_path(agent: str) -> Path:
    return get_config().data_dir / "active_context" / agent / "latest.md"


def render_active_context(agent: str, working_dir: str = "", user_message: str = "") -> str:
    timeout_sec = float(get_config().get("preflight.timeout_sec", 5))
    try:
        kia_context = _run_preflight_with_timeout(
            agent,
            working_dir or os.getcwd(),
            user_message or "",
            timeout_sec,
        )
    except TimeoutError:
        kia_context = (
            f"Mnemos preflight exceeded {timeout_sec:g}s and was skipped for startup responsiveness.\n\n"  # noqa: E501
            "Use the Mnemos MCP tools when relevant:\n"
            "- preflight_inject for task-scoped knowledge loading\n"
            "- session_search for raw quotes, evidence, and chat history\n"
            "- context_aware_search or wiki_search for knowledge lookup\n"
            "- both session_search and context_aware_search for mixed recall\n"
            "- check_pending_recaps for recap/follow-up nudges\n"
            "- predictive_push for timely related knowledge suggestions\n"
            "- guard_check before finalizing high-impact answers\n"
        )
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        kia_context = f"Mnemos preflight failed: {exc}"
    now = datetime.now().isoformat(timespec="seconds")
    return "\n".join(
        [
            "# Mnemos Active Context",
            "",
            f"- Agent: {agent}",
            f"- Working directory: {working_dir or os.getcwd()}",
            f"- Generated at: {now}",
            "",
            "## Use This",
            "",
            "Use the following Mnemos knowledge before planning or answering. "
            "If it conflicts with the current task, explain the conflict and prefer the user's latest explicit instruction.",  # noqa: E501
            "",
            "## Active Policy",
            "",
            active_policy_text(agent).strip(),
            "",
            "## Context",
            "",
            kia_context.strip() or "No relevant Mnemos context was found for this session.",
            "",
        ]
    )


def _full_preflight(agent: str, working_dir: str, user_message: str) -> str:
    """Run the full agent preflight path via Apollon.

    Prints from the synchronous Claude hook are captured here so they do not
    leak into the host agent's stdout regardless of whether we run in the main
    thread (SIGALRM) or in a worker thread (ThreadPoolExecutor).
    """
    # Local import avoids a top-level circular dependency:
    # integrations.apollon already imports from integrations.active.
    from integrations.apollon import get_context_for_agent

    with redirect_stdout(StringIO()):
        return get_context_for_agent(agent, working_dir, user_message)


def _timeout_fallback_context(
    agent: str, working_dir: str, user_message: str, timeout_sec: float
) -> str:
    """Return lightweight preflight with a concise timeout note."""
    lightweight = _build_lightweight_preflight(agent, working_dir, user_message)
    note = (
        f"Mnemos full preflight exceeded {timeout_sec:g}s; "
        "falling back to lightweight startup context.\n\n"
        "Use the Mnemos MCP tools when relevant: preflight_inject, "
        "context_aware_search / wiki_search, check_pending_recaps, "
        "predictive_push, guard_check."
    )
    return f"{note}\n\n{lightweight}".strip()


def _run_preflight_with_timeout(
    agent: str, working_dir: str, user_message: str, timeout_sec: float
) -> str:
    mode = str(get_config().get("preflight.mode", "full")).strip().lower()
    if mode == "light":
        return _build_lightweight_preflight(agent, working_dir, user_message)

    if timeout_sec <= 0:
        return _full_preflight(agent, working_dir, user_message)

    # Unix：在主线程使用 SIGALRM 精确中断；非主线程只能走线程池超时。
    if hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread():
        previous_handler = signal.getsignal(signal.SIGALRM)

        def _raise_timeout(_signum, _frame):
            raise TimeoutError("Mnemos preflight timeout")

        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout_sec)
        try:
            return _full_preflight(agent, working_dir, user_message)
        except TimeoutError:
            # 完整路径超时：保持启动响应速度，回落到轻量预加载
            return _timeout_fallback_context(agent, working_dir, user_message, timeout_sec)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)

    # 非 Unix 平台（Windows）或非主线程：使用线程池超时作为回退
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_full_preflight, agent, working_dir, user_message)
        try:
            return future.result(timeout=timeout_sec)
        except FutureTimeoutError:
            return _timeout_fallback_context(agent, working_dir, user_message, timeout_sec)


def _build_lightweight_preflight(agent: str, working_dir: str, user_message: str) -> str:
    """Build startup context without backend API reads.

    Session-start needs to feel instant. Deeper historical recall remains
    available through MCP tools such as session_search and context_aware_search.
    """
    return build_lightweight_preflight(agent, working_dir, user_message)


def write_active_context(
    agent: str, working_dir: str = "", user_message: str = ""
) -> Tuple[Path, str]:
    text = render_active_context(agent, working_dir, user_message)
    path = active_context_path(agent)
    write_text_if_changed(path, text, backup=False)
    return path, text


def generated_wrapper(agent: str) -> str:
    root = mnemos_root()
    return f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mnemos active bridge wrapper for {agent}."""

import sys

sys.path.insert(0, {str(root)!r})

from integrations.active_bridge import main


if __name__ == "__main__":
    main({agent!r})
'''


def wrapper_uses_active_bridge(path: Path) -> bool:
    return path.exists() and "integrations.active_bridge" in path.read_text(
        encoding="utf-8", errors="ignore"
    )
