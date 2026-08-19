from __future__ import annotations

import sys
from pathlib import Path

from core.agent_kit.shadow_eval import AgentShadowConfigStore, run_agent_shadow_eval


def _write_agent_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "shadow_agent.py"
    script.write_text(body, encoding="utf-8")
    return script


def test_agent_shadow_config_defaults_off_and_disables_content_send(tmp_path: Path):
    store = AgentShadowConfigStore(tmp_path / "agent_authorization.db")

    result = run_agent_shadow_eval(config_store=store, confirm_send_content=True)

    assert store.get().enabled is False
    assert result["ok"] is False
    assert result["status"] == "disabled"
    assert result["metrics"]["shadow_write_count"] == 0


def test_agent_shadow_eval_passes_with_explicit_single_agent_shadow(tmp_path: Path):
    script = _write_agent_script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.read())
print(json.dumps({
    "summary": request["prompt"][:32],
    "keywords": ["shadow", "eval"],
    "risk_level": "low",
}))
""".strip(),
    )
    store = AgentShadowConfigStore(tmp_path / "agent_authorization.db")
    config = store.enable(
        agent="codex",
        command=[sys.executable, str(script)],
        timeout_seconds=2,
        allowed_dirs=[tmp_path],
    )

    result = run_agent_shadow_eval(config_store=store, confirm_send_content=True)

    assert config.enabled is True
    assert config.agent == "codex"
    assert result["ok"] is True
    assert result["agent"] == "codex"
    assert result["metrics"]["schema_success_rate"] == 1.0
    assert result["metrics"]["fallback_rate"] == 0.0
    assert result["metrics"]["shadow_write_count"] == 0
    assert all(case["shadow_ok"] for case in result["cases"])


def test_agent_shadow_eval_requires_explicit_content_confirmation(tmp_path: Path):
    script = _write_agent_script(tmp_path, "print('{}')\n")
    store = AgentShadowConfigStore(tmp_path / "agent_authorization.db")
    store.enable(
        agent="codex",
        command=[sys.executable, str(script)],
        timeout_seconds=2,
        allowed_dirs=[tmp_path],
    )

    result = run_agent_shadow_eval(config_store=store, confirm_send_content=False)

    assert result["ok"] is False
    assert result["status"] == "disabled"
    assert "confirmation required" in result["reason"]


def test_agent_shadow_eval_records_schema_failures_as_fallbacks(tmp_path: Path):
    script = _write_agent_script(tmp_path, "print('not-json')\n")
    store = AgentShadowConfigStore(tmp_path / "agent_authorization.db")
    store.enable(
        agent="codex",
        command=[sys.executable, str(script)],
        timeout_seconds=2,
        allowed_dirs=[tmp_path],
    )

    result = run_agent_shadow_eval(config_store=store, confirm_send_content=True)

    assert result["ok"] is False
    assert result["status"] == "threshold_failed"
    assert result["metrics"]["schema_success_rate"] == 0.0
    assert result["metrics"]["fallback_rate"] == 1.0
    assert all(case["fallback_to_baseline"] for case in result["cases"])


def test_agent_shadow_config_keeps_single_active_agent_and_disable_revokes(tmp_path: Path):
    script = _write_agent_script(tmp_path, "print('{}')\n")
    store = AgentShadowConfigStore(tmp_path / "agent_authorization.db")
    store.enable(
        agent="codex",
        command=[sys.executable, str(script)],
        allowed_dirs=[tmp_path],
    )
    replaced = store.enable(
        agent="kimi",
        command=[sys.executable, str(script)],
        allowed_dirs=[tmp_path],
    )
    disabled = store.disable()

    assert replaced.agent == "kimi"
    assert replaced.enabled is True
    assert disabled.enabled is False
    assert disabled.agent == "kimi"
