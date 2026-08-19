"""Cross-process keyring backend used only by MCP stdio integration tests."""

from __future__ import annotations

import json
import os
import site
import stat
from pathlib import Path
from typing import Dict

from core.agent_kit.authorization import MCPLaunchCredentialStore


def configure_test_keyring_environment(
    tmp_path: Path,
    *,
    agent: str,
    credential: str,
) -> tuple[Dict[str, str], str]:
    """Return a subprocess env whose keyring resolves one test reference."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    reference = MCPLaunchCredentialStore.reference_for(agent, credential)
    account = reference[len(MCPLaunchCredentialStore.reference_prefix) :]
    secrets_path = tmp_path / "test-keyring.json"
    secrets_path.write_text(
        json.dumps({account: credential}),
        encoding="utf-8",
    )
    secrets_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        "import json, os\n"
        "import keyring\n"
        "from keyring.backend import KeyringBackend\n"
        "class TestKeyring(KeyringBackend):\n"
        "    priority = 1\n"
        "    def get_password(self, service, username):\n"
        "        if service != 'mnemos.mcp.launch': return None\n"
        "        with open(os.environ['MNEMOS_TEST_KEYRING_FILE'], encoding='utf-8') as f:\n"
        "            return json.load(f).get(username)\n"
        "    def set_password(self, service, username, password): raise RuntimeError('read only')\n"
        "    def delete_password(self, service, username): raise RuntimeError('read only')\n"
        "keyring.set_keyring(TestKeyring())\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    interpreter_site_paths = [*site.getsitepackages(), site.getusersitepackages()]
    env["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (str(tmp_path), existing_pythonpath, *interpreter_site_paths)
        if part
    )
    env["MNEMOS_TEST_KEYRING_FILE"] = str(secrets_path)
    env["MNEMOS_MCP_LAUNCH_CAPABILITY_REF"] = reference
    env.pop("MNEMOS_MCP_LAUNCH_CAPABILITY", None)
    return env, reference
