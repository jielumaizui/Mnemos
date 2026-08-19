# -*- coding: utf-8 -*-
"""User-risk acceptance tests for Mnemos.

These tests intentionally cover user-visible failure modes across modules.
They are lightweight and use temp dirs/mocks so they can run in normal pytest.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FakeConfig:
    def __init__(self, data=None, *, base_dir: Path | None = None):
        self.data = data or {}
        self.database_dir = (base_dir or Path("/tmp")) / "data"
        self.wiki_dir = (base_dir or Path("/tmp")) / "wiki"
        self.obsidian_vault_path = (base_dir or Path("/tmp")) / "raw"

    def get(self, key, default=None):
        value = self.data
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value


def clear_model_api_env(monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "SILICONFLOW_API_KEY",
        "SILICONFLOW_BASE_URL",
        "SILICONFLOW_MODEL",
        "DMXAPI_API_KEY",
        "DMX_API_KEY",
        "DMXAPI_BASE_URL",
        "DMXAPI_MODEL",
        "MNEMOS_LLM_API_KEY",
        "MNEMOS_LLM_BASE_URL",
        "MNEMOS_LLM_MODEL",
        "MNEMOS_LLM_PROVIDER",
        "MNEMOS_EMBEDDING_API_KEY",
        "MNEMOS_EMBEDDING_BASE_URL",
        "MNEMOS_EMBEDDING_MODEL",
        "MNEMOS_EMBEDDING_PROVIDER",
        "MNEMOS_RERANKER_API_KEY",
        "MNEMOS_RERANKER_BASE_URL",
        "MNEMOS_RERANKER_MODEL",
        "MNEMOS_RERANKER_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)


def test_privacy_and_access_acceptance(tmp_path):
    from core.access_policy import AccessContext, filter_readable_items
    from core.telemetry.prompt_call_log import (
        ModelCallLedger,
        ModelCallLedgerInvariantError,
        PromptCallLog,
    )

    readable, summary = filter_readable_items(
        [
            {
                "page_id": "private-note",
                "scope": "private",
                "source_agent": "claude",
                "session_id": "s1",
            }
        ],
        AccessContext(agent="codex", session_id="s1"),
    )

    assert readable == []
    assert summary["private_cross_agent_denied"] == 1

    provider_shaped_key = "sk" + "-1234567890abcdef"
    api_key_label = "api_" + "key"
    token_label = "token"
    password_error = "password" + "=" + "DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST"
    db_path = tmp_path / "model_call_ledger.db"
    with pytest.raises(ModelCallLedgerInvariantError, match="PromptCallLog is retired"):
        PromptCallLog(db_path)

    class LedgerConfig:
        data_dir = tmp_path
        database_dir = tmp_path

        @staticmethod
        def get(key, default=None):
            if key == "llm.provider_prices":
                return {"test": {"model": {"input": 0.1, "output": 0.2}}}
            return default

    provider_payload = (
        f"Use {api_key_label}=super-secret-value and {token_label}=another-secret "
        f"{provider_shaped_key}"
    )
    ledger = ModelCallLedger(db_path, config=LedgerConfig())
    run_id = ledger.start_run("privacy-acceptance", subject_scope=("session", "s1"))
    reservation = ledger.reserve(
        run_id=run_id,
        operation="distill",
        provider="test",
        model="model",
        input_text=provider_payload,
        input_tokens=len(provider_payload.encode("utf-8")),
    )
    reservation.release(error_code=password_error)

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM model_call_entries").fetchone()
        columns = [item[1] for item in conn.execute("PRAGMA table_info(model_call_entries)").fetchall()]

    serialized = " ".join(str(value) for value in dict(row).values())
    assert "prompt" not in columns
    assert "prompt_summary" not in columns
    assert "prompt_preview" not in columns
    assert "response_preview" not in columns
    assert "super-secret-value" not in serialized
    assert "another-secret" not in serialized
    assert provider_shaped_key not in serialized
    assert "DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST" not in serialized


def test_no_write_dry_run_acceptance(tmp_path, monkeypatch):
    from scripts import e2e_probe

    cfg = FakeConfig(base_dir=tmp_path)
    cfg.database_dir.mkdir(parents=True)
    cfg.wiki_dir.mkdir(parents=True)
    cfg.obsidian_vault_path.mkdir(parents=True)
    monkeypatch.setattr("core.config.get_config", lambda: cfg)

    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    probes = [
        e2e_probe._probe_dry_run_config,
        e2e_probe._probe_dry_run_databases,
        e2e_probe._probe_dry_run_llm_config,
    ]

    for probe in probes:
        status, message = probe()
        assert status in {e2e_probe.STATUS_PASS, e2e_probe.STATUS_SKIP}, message

    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert after == before


def test_doctor_command_executable_acceptance():
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "mnemos_cli.py"), "doctor", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "--json" in result.stdout
    assert "--e2e" in result.stdout


def test_daemon_heartbeat_acceptance(tmp_path, monkeypatch):
    from core.ops.health_check import _check_heartbeat
    from daemon import instance_identity, intervals

    cfg = FakeConfig(base_dir=tmp_path)
    cfg.database_dir.mkdir(parents=True)
    fingerprint = instance_identity.ProcessFingerprint(
        pid=4242,
        pid_start_time="start-1",
        boot_id="boot-1",
        executable=sys.executable,
        command_line=f"{sys.executable} {PROJECT_ROOT / 'mnemos_daemon.py'} start",
    )
    service_names = intervals.build_default_intervals(capture_tick=300)
    identity = instance_identity.create_instance_record(
        database_dir=cfg.database_dir,
        service_names=service_names,
        project_root=PROJECT_ROOT,
        process_fingerprint=fingerprint,
        instance_id="acceptance-instance",
    )
    monkeypatch.setattr(
        instance_identity,
        "inspect_process",
        lambda _pid: fingerprint,
    )
    (cfg.database_dir / "daemon.pid").write_text(
        json.dumps(identity),
        encoding="utf-8",
    )
    services = {
        name: {"ok": True, "enabled": True}
        for name in service_names
    }
    (cfg.database_dir / "daemon_heartbeat.json").write_text(
        json.dumps(
            {
                "schema_version": instance_identity.HEARTBEAT_SCHEMA_VERSION,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "instance_identity": identity,
                "services": services,
                "service_errors": {},
            }
        ),
        encoding="utf-8",
    )

    result = _check_heartbeat(cfg)

    assert result["status"] == "ok"
    assert result["running"] is True
    assert result["identity_match"] is True
    assert result["services_count"] == len(service_names)
    assert result["services"]["capture_worker"]["ok"] is True


def test_distill_golden_output_acceptance():
    from core.hephaestus.distillation_engine import (
        DistillSelfCheck,
        KnowledgeFragment,
        _strict_validate_fragments,
    )

    fragment = KnowledgeFragment(
        form="decision",
        title="Redis 集群故障转移策略沉淀",
        frontmatter={
            "摘要": "记录 Redis 集群故障转移策略的稳定决策",
            "领域": "系统架构",
        },
        background="一次架构复盘后沉淀的稳定决策。",
        core_content=(
            "# Redis 集群故障转移策略沉淀\n\n"
            "在生产集群中，主从切换必须由哨兵或托管控制面统一触发，"
            "业务服务只依赖连接池的重连和超时策略。验收时需要观察切换期间"
            "请求错误率、恢复时间和写入一致性。可接受标准是 30 秒内恢复、"
            "写请求错误率低于 1%、恢复后抽样 100 条关键记录一致，避免把一次局部恢复误判为系统可靠。"
        ),
        boundaries={"applies": "Redis 主从或哨兵部署", "not_applies": "单机缓存"},
        anti_patterns=[],
        related_concepts=["故障转移", "连接池"],
    )

    strict_ok, strict_issues = _strict_validate_fragments([fragment])
    self_ok, self_issues = DistillSelfCheck().check([fragment], [])

    assert strict_ok is True, strict_issues
    assert self_ok is True, self_issues
    assert fragment.self_check_severity == "ok"


def test_provider_configuration_acceptance(monkeypatch):
    from scripts.verify_installation import check_model_api_config

    clear_model_api_env(monkeypatch)
    monkeypatch.setenv("ACCEPTANCE_LLM_KEY", "llm-key")
    monkeypatch.setenv("ACCEPTANCE_EMBEDDING_KEY", "embedding-key")
    monkeypatch.setenv("ACCEPTANCE_RERANKER_KEY", "reranker-key")

    cfg = FakeConfig(
        {
            "llm": {
                "provider": "openai-compatible",
                "api_key_source": "env:ACCEPTANCE_LLM_KEY",
                "base_url": "https://gateway.example.test/v1",
                "model": "custom-chat-model",
            },
            "embedding": {
                "provider": "openai-compatible",
                "api_key_source": "env:ACCEPTANCE_EMBEDDING_KEY",
                "base_url": "https://gateway.example.test/v1",
                "model": "custom-embedding-model",
            },
            "reranker": {
                "provider": "openai-compatible",
                "api_key_source": "env:ACCEPTANCE_RERANKER_KEY",
                "base_url": "https://gateway.example.test/v1",
                "model": "custom-reranker-model",
            },
        }
    )

    result = check_model_api_config(config=cfg, api_smoke=False)

    assert result["ok"] is True
    assert result["apis"]["llm"]["status"] == "configured"
    assert result["apis"]["embedding"]["model"] == "custom-embedding-model"
    assert result["apis"]["reranker"]["base_url"] == "https://****/v1"
    assert result["apis"]["reranker"]["source"] == "env:****"
