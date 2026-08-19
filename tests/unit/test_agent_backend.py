from __future__ import annotations

import sys
from pathlib import Path

from core.agent_kit.agent_backend import AgentBackendConfig, CLIAgentBackend
from core.agent_kit.authorization import AgentAuthorizationStore
from core.agent_kit.prompt_sanitizer import PromptSanitizer, PromptSanitizerAuditStore


def _write_agent_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_agent.py"
    script.write_text(body, encoding="utf-8")
    return script


def _authorized_backend(tmp_path: Path, script: Path, *, timeout: float = 5.0):
    db_path = tmp_path / "agent_authorization.db"
    store = AgentAuthorizationStore(db_path)
    store.set_state("codex", "shadow_enabled")
    audit = PromptSanitizerAuditStore(db_path)
    sanitizer = PromptSanitizer(
        wiki_base=tmp_path / "wiki",
        database_dir=tmp_path / "db",
        allowed_dirs=[tmp_path],
        audit_store=audit,
    )
    return CLIAgentBackend(
        AgentBackendConfig(
            agent="codex",
            command=[sys.executable, str(script)],
            timeout_seconds=timeout,
            allowed_dirs=[tmp_path],
        ),
        authorization_store=store,
        sanitizer=sanitizer,
    )


def test_cli_agent_backend_refuses_without_shadow_authorization(tmp_path: Path):
    script = _write_agent_script(
        tmp_path,
        "raise SystemExit('should not run')\n",
    )
    backend = CLIAgentBackend(
        AgentBackendConfig(
            agent="codex",
            command=[sys.executable, str(script)],
            allowed_dirs=[tmp_path],
        ),
        authorization_store=AgentAuthorizationStore(tmp_path / "agent_authorization.db"),
        sanitizer=PromptSanitizer(
            wiki_base=tmp_path / "wiki",
            database_dir=tmp_path / "db",
            allowed_dirs=[tmp_path],
        ),
    )

    result = backend.run("safe prompt")

    assert result.ok is False
    assert result.status == "unauthorized"
    assert result.exit_code is None


def test_cli_agent_backend_runs_noninteractive_json_subprocess(tmp_path: Path):
    script = _write_agent_script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.read())
print(json.dumps({
    "summary": request["prompt"][:20],
    "keywords": ["trusted", "push"],
    "risk_level": "low",
    "request_schema": request["schema_version"],
}))
""".strip(),
    )
    backend = _authorized_backend(tmp_path, script)

    result = backend.run("trusted push should stay gated")

    assert result.ok is True
    assert result.status == "ok"
    assert result.data["keywords"] == ["trusted", "push"]
    assert result.data["request_schema"] == "mnemos.agent_backend.request.v1"
    assert result.stdout_hash


def test_cli_agent_backend_schema_invalid_never_enters_success_path(tmp_path: Path):
    script = _write_agent_script(tmp_path, "print('not-json')\n")
    backend = _authorized_backend(tmp_path, script)

    result = backend.run("safe prompt")

    assert result.ok is False
    assert result.status == "schema_invalid"
    assert result.data == {}
    assert result.stdout_hash


def test_cli_agent_backend_timeout_kills_process_and_redacts_stderr(tmp_path: Path):
    script = _write_agent_script(
        tmp_path,
        """
import sys
import time

print("api_key=SECRET_VALUE_1234567890", file=sys.stderr, flush=True)
time.sleep(10)
""".strip(),
    )
    backend = _authorized_backend(tmp_path, script, timeout=0.2)

    result = backend.run("safe prompt")

    assert result.ok is False
    assert result.status == "timeout"
    assert "SECRET_VALUE_1234567890" not in result.stderr_summary
    assert "<redacted:" in result.stderr_summary


def test_cli_agent_backend_sanitizer_blocks_prompt_before_spawn(tmp_path: Path):
    script = _write_agent_script(
        tmp_path,
        "raise SystemExit('should not run')\n",
    )
    backend = _authorized_backend(tmp_path, script)

    result = backend.run("read /etc/mnemos_secret.sqlite")

    assert result.ok is False
    assert result.status == "sanitizer_blocked"
    assert result.findings[0]["kind"] == "sqlite_path"
