#!/usr/bin/env python3
"""
Mnemos 部署后验证脚本 — 确认安装完整、核心链路可运转

用法:
    python3 scripts/verify_installation.py [--full]

选项:
    --full  同时运行集成测试（需要 pytest）
    --api-smoke  对已配置的 LLM/embedding/reranker 做真实 API smoke（默认不联网）
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DOCTOR_TIMEOUT_SECONDS = 60

VERIFY_OPERATION_ERRORS = (
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.Error,
    subprocess.SubprocessError,
)
if __name__ == "__main__":
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))  # [P2-FIX] Guard sys.path mutation


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _err(msg: str) -> None:
    print(f"  ✗ {msg}")


def _warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def _step(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print("=" * 60)


def check_python_version() -> bool:
    v = sys.version_info
    if v >= (3, 10):
        _ok(f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    _err(f"Python {v.major}.{v.minor} < 3.10")
    return False


def check_compileall() -> bool:
    try:
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "core",
                "integrations",
                "mnemos_cli.py",
                "mnemos_daemon.py",
                "scripts",
                "tests",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=120,  # [P1-FIX] Add timeout to prevent indefinite hang
        )
        if r.returncode == 0:
            _ok("compileall 通过（无语法错误）")
            return True
        _err("compileall 失败")
        return False
    except subprocess.TimeoutExpired:
        _err("compileall 超时（>120s）")
        return False
    except VERIFY_OPERATION_ERRORS as e:
        _err(f"compileall 异常: {e}")
        return False


def check_cli_help() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "mnemos_cli.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0 and "Mnemos" in r.stdout:
            _ok("CLI 可执行")
            return True
        _err("CLI --help 失败")
        return False
    except VERIFY_OPERATION_ERRORS as e:
        _err(f"CLI 异常: {e}")
        return False


def check_daemon_import() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, "-c", "from mnemos_daemon import main; print('daemon_import_ok')"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "daemon_import_ok" in r.stdout:
            _ok("Daemon 可导入")
            return True
        _err("Daemon 导入失败")
        return False
    except VERIFY_OPERATION_ERRORS as e:
        _err(f"Daemon 导入异常: {e}")
        return False


def check_doctor() -> dict:
    result: dict[str, Any] = {"ok": False, "warnings": [], "errors": []}
    try:
        r = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "mnemos_cli.py"),
                "doctor",
                "--json",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=DOCTOR_TIMEOUT_SECONDS,
        )
        stdout = r.stdout + r.stderr
        try:
            doctor_payload = json.loads(r.stdout)
        except json.JSONDecodeError:
            doctor_payload = {}
        if (isinstance(doctor_payload, dict) and doctor_payload) or "知识库健康度" in stdout:
            _ok("doctor 可运行")
            result["ok"] = True
        else:
            _err("doctor 输出异常")
            result["errors"].append("doctor 未输出预期内容")

        # 提取警告
        for line in stdout.splitlines():
            if "⚠" in line or "警告" in line or "warning" in line.lower():
                result["warnings"].append(line.strip())
        if result["warnings"]:
            _warn(f"doctor 发现 {len(result['warnings'])} 条警告")
    except VERIFY_OPERATION_ERRORS as e:
        _err(f"doctor 运行异常: {e}")
        result["errors"].append(str(e))
    return result


def check_integration_tests(full: bool = False) -> dict:
    result = {
        "ok": False,
        "status": "pending",
        "passed": 0,
        "failed": 0,
        "skipped": False,
        "required_for_full_verification": True,
    }
    if not full:
        _warn("跳过集成测试（使用 --full 启用）")
        result["status"] = "skipped"
        result["skipped"] = True
        result["reason"] = "use --full to run core integration tests"
        return result

    try:
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/integration/test_distill_to_kg_event_path.py",
                "tests/integration/test_worker_kg_event_path.py",
                "tests/integration/test_distill_worker_loop.py",
                "-v",
                "--tb=short",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        stdout = r.stdout
        if "passed" in stdout:
            import re

            m = re.search(r"(\d+) passed", stdout)
            if m:
                result["passed"] = int(m.group(1))
            m = re.search(r"(\d+) failed", stdout)
            if m:
                result["failed"] = int(m.group(1))
            if result["failed"] == 0:
                _ok(f"核心集成测试通过: {result['passed']} passed")
                result["ok"] = True
                result["status"] = "passed"
            else:
                _err(f"集成测试失败: {result['failed']} failed")
                result["status"] = "failed"
        else:
            _err("集成测试未产生有效输出")
            result["status"] = "invalid_output"
    except VERIFY_OPERATION_ERRORS as e:
        _err(f"集成测试运行异常: {e}")
        result["status"] = "error"
        result["error"] = str(e)
    return result


def check_db_writable(*, write_probe: bool = False) -> bool:
    try:
        from core.config import get_config

        cfg = get_config()
        test_paths = [
            cfg.data_dir,
            cfg.wiki_dir,
        ]
        all_ok = True
        for p in test_paths:
            if not p.exists() or not p.is_dir():
                _err(f"目录不存在: {p}")
                all_ok = False
            else:
                try:
                    if write_probe:
                        _write_probe_file(p)
                    elif not os.access(p, os.R_OK):
                        raise PermissionError(f"目录不可读: {p}")
                except VERIFY_OPERATION_ERRORS as e:
                    operation = "读写探针" if write_probe else "只读检查"
                    _err(f"目录{operation}失败: {p} ({e})")
                    all_ok = False
        if all_ok:
            _ok("数据目录可访问" + ("且写探针通过" if write_probe else "（只读）"))
        return all_ok
    except VERIFY_OPERATION_ERRORS as e:
        _err(f"目录检查异常: {e}")
        return False


def _path_accessible(path: Path, *, write_probe: bool = False) -> bool:
    try:
        if not path.exists() or not path.is_dir():
            return False
        if write_probe:
            _write_probe_file(path)
            return True
        return os.access(path, os.R_OK)
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
        subprocess.SubprocessError
    ):
        return False


def _path_writable(path: Path) -> bool:
    """Compatibility helper for callers that explicitly request a write probe."""
    return _path_accessible(path, write_probe=True)


def _write_probe_file(path: Path) -> None:
    test_file = path / f".mnemos_write_test.{uuid.uuid4().hex}"
    try:
        test_file.write_text("ok", encoding="utf-8")
    finally:
        test_file.unlink(missing_ok=True)


def check_obsidian_and_vaults(*, write_probe: bool = False) -> dict:
    """检查 Obsidian 应用和 Mnemos/raw vault。

    Obsidian 应用本身是推荐依赖，不要求 CI 或无头环境必须安装；
    vault 可写性是 Mnemos 基础能力，必须通过。
    """
    result: dict[str, Any] = {
        "ok": True,
        "obsidian": {"installed": False, "version": None, "path": None},
        "vaults": {},
        "warnings": [],
        "errors": [],
    }

    try:
        from scripts.auto_setup import detect_obsidian_app

        installed, version, app_path = detect_obsidian_app()
        result["obsidian"] = {
            "installed": installed,
            "version": version,
            "path": str(app_path) if app_path else None,
        }
        if installed:
            version_text = f" v{version}" if version else ""
            _ok(f"Obsidian 已安装{version_text} ({app_path})")
        else:
            warning = "未检测到 Obsidian 应用，请从 https://obsidian.md/download 安装最新版"
            result["warnings"].append(warning)
            _warn(warning)
    except VERIFY_OPERATION_ERRORS as e:
        warning = f"Obsidian 检测失败: {e}"
        result["warnings"].append(warning)
        _warn(warning)

    try:
        from core.config import get_config
        from core.setup.vault_layout import list_mnemos_dirs

        cfg = get_config()
        vault_specs = {
            "mnemos": cfg.vault_dir("mnemos"),
            "raw": cfg.vault_dir("raw"),
        }
        for name, path in vault_specs.items():
            marker = path / ".obsidian"
            writable = _path_accessible(path, write_probe=write_probe)
            marker_exists = marker.exists()
            standard_dirs: dict[str, Any] = {}
            if name == "mnemos":
                expected_dirs = list(list_mnemos_dirs())
                missing_dirs = [d for d in expected_dirs if not (path / d).is_dir()]
                standard_dirs = {
                    "checked": len(expected_dirs),
                    "missing": missing_dirs,
                }
            result["vaults"][name] = {
                "path": str(path),
                "exists": path.exists(),
                "writable": writable,
                "obsidian_marker": marker_exists,
                "standard_dirs": standard_dirs,
            }
            if standard_dirs.get("missing"):
                missing = standard_dirs["missing"]
                sample = ", ".join(missing[:5])
                suffix = "..." if len(missing) > 5 else ""
                error = f"{name} vault 缺少标准目录: {sample}{suffix}"
                result["errors"].append(error)
                result["ok"] = False
                _err(error)
            elif writable and marker_exists:
                _ok(f"{name} vault 可写: {path}")
            elif writable:
                warning = f"{name} vault 可写但缺少 .obsidian 标记: {path}"
                result["warnings"].append(warning)
                _warn(warning)
            else:
                error = f"{name} vault 不可写: {path}"
                result["errors"].append(error)
                result["ok"] = False
                _err(error)
    except VERIFY_OPERATION_ERRORS as e:
        error = f"Vault 检查异常: {e}"
        result["errors"].append(error)
        result["ok"] = False
        _err(error)

    return result


def _smoke_ledger(
    config: Any | None,
    operation: str,
    api_cfg: Any,
    *,
    input_text: str,
    output_tokens: int,
):
    """Reserve an explicit opt-in network smoke before it reaches a provider."""
    from core.telemetry.prompt_call_log import ModelCallLedger
    from core.telemetry.provider_request import utf8_token_upper_bound

    ledger = ModelCallLedger.for_config(config)
    subject_scope = ("source", "verify_installation")
    run_id = ledger.start_run(
        f"verify-{operation}:{uuid.uuid4().hex}",
        subject_scope=subject_scope,
    )
    return ledger.reserve(
        run_id=run_id,
        operation=operation,
        provider=str(getattr(api_cfg, "provider", "") or "openai-compatible"),
        model=str(api_cfg.model),
        # The ledger retains only a digest, so it can safely bind its
        # reservation to the exact provider-visible request representation.
        input_text=input_text,
        input_tokens=utf8_token_upper_bound(input_text),
        output_tokens=output_tokens,
        cache_status="miss",
        subject_scopes=(subject_scope,),
    )


def _smoke_llm_api(api_cfg: Any, *, config: Any | None = None) -> tuple[bool, str]:
    from openai import OpenAI
    from core.telemetry.prompt_call_log import metered_provider_usage
    from core.telemetry.provider_request import (
        canonical_chat_input,
        non_redirecting_openai_client,
    )

    messages = [{"role": "user", "content": "ping"}]
    reservation = _smoke_ledger(
        config,
        "verify_llm_smoke",
        api_cfg,
        input_text=canonical_chat_input(messages),
        output_tokens=1,
    )
    try:
        reservation.mark_dispatched()
        started = time.perf_counter()
        with non_redirecting_openai_client(
            OpenAI,
            api_key=api_cfg.api_key,
            base_url=api_cfg.base_url,
        ) as client:
            response = client.chat.completions.create(
                model=api_cfg.model,
                messages=messages,
                max_tokens=1,
                timeout=getattr(api_cfg, "timeout", 60),
            )
        usage = getattr(response, "usage", None)
        request_id = str(getattr(response, "id", "") or "")
        metered_usage = metered_provider_usage(
            usage,
            request_id=request_id,
            output_required=True,
        )
        if metered_usage is None:
            reservation.preserve_incurred(error_code="verify_llm_smoke_usage_missing")
        else:
            reservation.settle(
                usage=metered_usage,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
    except _smoke_exception_types():
        if reservation.dispatched:
            reservation.preserve_incurred(error_code="verify_llm_smoke_exception")
        else:
            reservation.release(error_code="verify_llm_smoke_pre_dispatch_exception")
        raise
    return True, "ok"


def _smoke_embedding_api(api_cfg: Any, *, config: Any | None = None) -> tuple[bool, str]:
    from openai import OpenAI
    from core.telemetry.prompt_call_log import metered_provider_usage
    from core.telemetry.provider_request import (
        canonical_provider_input,
        non_redirecting_openai_client,
    )

    request_payload = {
        "model": api_cfg.model,
        "input": ["ping"],
        "encoding_format": "float",
    }
    reservation = _smoke_ledger(
        config,
        "verify_embedding_smoke",
        api_cfg,
        input_text=canonical_provider_input(request_payload),
        output_tokens=0,
    )
    try:
        reservation.mark_dispatched()
        started = time.perf_counter()
        with non_redirecting_openai_client(
            OpenAI,
            api_key=api_cfg.api_key,
            base_url=api_cfg.base_url,
        ) as client:
            response = client.embeddings.create(**request_payload)
        usage = getattr(response, "usage", None)
        request_id = str(getattr(response, "id", "") or "")
        metered_usage = metered_provider_usage(
            usage,
            request_id=request_id,
            output_required=False,
        )
        if metered_usage is None:
            reservation.preserve_incurred(error_code="verify_embedding_smoke_usage_missing")
        else:
            reservation.settle(
                usage=metered_usage,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
    except _smoke_exception_types():
        if reservation.dispatched:
            reservation.preserve_incurred(error_code="verify_embedding_smoke_exception")
        else:
            reservation.release(error_code="verify_embedding_smoke_pre_dispatch_exception")
        raise
    return True, "ok"


def _reranker_smoke_url(base_url: str) -> str:
    base = str(base_url or "").strip()
    if not base:
        return ""
    base = base.rstrip("/")
    if base.endswith("/rerank"):
        return base
    return urljoin(base + "/", "rerank")


def _smoke_reranker_api(api_cfg: Any, *, config: Any | None = None) -> tuple[bool, str]:
    import requests
    from core.telemetry.prompt_call_log import metered_provider_usage
    from core.telemetry.provider_request import canonical_provider_input

    payload = {
        "model": api_cfg.model,
        "query": "ping",
        "documents": ["pong"],
        "top_n": 1,
        "return_documents": False,
    }

    reservation = _smoke_ledger(
        config,
        "verify_rerank_smoke",
        api_cfg,
        input_text=canonical_provider_input(payload),
        output_tokens=0,
    )
    try:
        reservation.mark_dispatched()
        started = time.perf_counter()
        resp = requests.post(
            _reranker_smoke_url(api_cfg.base_url),
            headers={
                "Authorization": f"Bearer {api_cfg.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
            allow_redirects=False,
        )
        status_code = getattr(resp, "status_code", None)
        if isinstance(status_code, int) and 300 <= status_code < 400:
            raise requests.HTTPError("reranker smoke provider redirect response rejected")
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") if isinstance(data, dict) else None
        request_id = str(
            (data.get("request_id") if isinstance(data, dict) else "")
            or (data.get("id") if isinstance(data, dict) else "")
            or getattr(resp, "headers", {}).get("x-request-id", "")
            or getattr(resp, "headers", {}).get("request-id", "")
            or ""
        )
        metered_usage = metered_provider_usage(
            usage,
            request_id=request_id,
            output_required=False,
        )
        if metered_usage is None:
            reservation.preserve_incurred(error_code="verify_rerank_smoke_usage_missing")
        else:
            reservation.settle(
                usage=metered_usage,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
    except _smoke_exception_types():
        if reservation.dispatched:
            reservation.preserve_incurred(error_code="verify_rerank_smoke_exception")
        else:
            reservation.release(error_code="verify_rerank_smoke_pre_dispatch_exception")
        raise
    return True, "ok"


def _smoke_multimodal_api(api_cfg: Any, *, config: Any | None = None) -> tuple[bool, str]:
    return _smoke_llm_api(api_cfg, config=config)


def _smoke_exception_types() -> tuple[type[BaseException], ...]:
    exceptions: list[type[BaseException]] = [OSError, RuntimeError, ValueError, TypeError]
    try:
        from openai import OpenAIError

        exceptions.append(OpenAIError)
    except ImportError:
        pass
    try:
        import requests

        exceptions.append(requests.RequestException)
    except ImportError:
        pass
    return tuple(exceptions)


def check_model_api_config(
    config: Any | None = None,
    api_smoke: bool = False,
    *,
    show_sensitive: bool = False,
) -> dict:
    """检查必填模型与可选多模态模型配置状态。

    默认只解析配置，不做网络请求；传入 api_smoke=True 时，对已配置项做真实
    OpenAI-compatible smoke，用于区分“配置但不可达”和“可用”。
    """
    result: dict[str, Any] = {"ok": True, "apis": {}, "warnings": [], "errors": []}
    try:
        if config is None:
            from core.config import get_config

            config = get_config()
        from core.llm_config import (
            resolve_embedding_api_config,
            resolve_effective_llm_api_config,
            resolve_multimodal_api_config,
            resolve_reranker_api_config,
        )
        from core.ops.health_check import summarize_model_api_config
        from core.telemetry.provider_request import safe_provider_error_category

        checks = [
            ("llm", "LLM", resolve_effective_llm_api_config(config), _smoke_llm_api),
            (
                "embedding",
                "Embedding",
                resolve_embedding_api_config(config),
                _smoke_embedding_api,
            ),
            ("reranker", "Reranker", resolve_reranker_api_config(config), _smoke_reranker_api),
        ]
        optional_checks = [
            (
                "multimodal",
                "Multimodal",
                resolve_multimodal_api_config(config),
                _smoke_multimodal_api,
            )
        ]

        for key, label, api_cfg, smoke_fn in checks:
            summary = summarize_model_api_config(
                api_cfg,
                key,
                show_sensitive=show_sensitive,
            )
            result["apis"][key] = summary
            details = (
                f"provider={summary['provider']}, model={summary['model']}, "
                f"base_url={summary['base_url']}, source={summary['source']}"
            )

            if summary["status"] != "configured":
                result["ok"] = False
                if summary["status"] == "incomplete":
                    warning = (
                        f"{label} API 配置不完整：必须提供可解析 api key、model、base_url"
                        f"（{details}）"
                    )
                else:
                    warning = f"{label} API 未配置（{details}）"
                result["warnings"].append(warning)
                _warn(warning)
                continue

            if not api_smoke:
                summary["status"] = "configured"
                _ok(f"{label} API 已配置（{details}，未联网 smoke）")
                continue

            try:
                ok, message = smoke_fn(api_cfg, config=config)
            except _smoke_exception_types() as e:
                ok, message = False, safe_provider_error_category(e)
            if ok:
                summary["status"] = "available"
                _ok(f"{label} API 可用（{details}）")
            else:
                summary["status"] = "unreachable"
                summary["error"] = message
                result["errors"].append(f"{label} API 不可达: {message}")
                result["ok"] = False
                _err(f"{label} API 不可达: {message}")

        for key, label, api_cfg, smoke_fn in optional_checks:
            summary = summarize_model_api_config(
                api_cfg,
                key,
                show_sensitive=show_sensitive,
            )
            summary["optional"] = True
            result["apis"][key] = summary
            details = (
                f"provider={summary['provider']}, model={summary['model']}, "
                f"base_url={summary['base_url']}, source={summary['source']}"
            )
            if summary["status"] != "configured":
                if summary["status"] == "not_configured":
                    summary["status"] = "skipped"
                    warning = f"{label} API 未配置，已跳过（可选；{details}）"
                else:
                    warning = (
                        f"{label} API 配置不完整，已按可恢复降级处理"
                        f"（可选；{details}）"
                    )
                result["warnings"].append(warning)
                _warn(warning)
                continue

            if not api_smoke:
                summary["status"] = "configured"
                _ok(f"{label} API 已配置（可选；{details}，未联网 smoke）")
                continue

            try:
                ok, message = smoke_fn(api_cfg, config=config)
            except _smoke_exception_types() as e:
                ok, message = False, safe_provider_error_category(e)
            if ok:
                summary["status"] = "available"
                _ok(f"{label} API 可用（可选；{details}）")
            else:
                summary["status"] = "unreachable"
                summary["error"] = message
                result["warnings"].append(f"{label} API 不可达（可选）: {message}")
                _warn(f"{label} API 不可达（可选）: {message}")
    except VERIFY_OPERATION_ERRORS as e:
        result["ok"] = False
        result["errors"].append(str(e))
        _err(f"模型 API 配置检查异常: {e}")

    return result


def check_agent_full_power() -> dict:
    """Check the complete eight-host Agent Kit full-power denominator."""
    result: dict[str, Any] = {
        "ok": True,
        "workflow_contract_ok": False,
        "conformance_ok": False,
        "full_power_ok": False,
        "installed_agents": [],
        "full_power_agents": [],
        "degraded_agents": [],
        "warnings": [],
        "errors": [],
        "agents": [],
    }
    try:
        from core.agent_kit import build_agent_kit_report

        report = build_agent_kit_report()
        payload = report.to_dict()
        result.update(
            {
                "workflow_contract_ok": report.workflow_contract_ok,
                "conformance_ok": report.conformance_ok,
                "full_power_ok": report.full_power_ok,
                "installed_agents": report.installed_agents,
                "full_power_agents": report.full_power_agents,
                "degraded_agents": report.degraded_agents,
                "runtime_unverified_agents": report.runtime_unverified_agents,
                "missing_workflow_tools": report.missing_workflow_tools,
                "agents": payload.get("agents", []),
            }
        )

        if report.workflow_contract_ok:
            _ok("Agent Kit workflow MCP 工具齐全")
        else:
            error = "Agent Kit workflow MCP 工具缺失: " + ", ".join(
                report.missing_workflow_tools
            )
            result["ok"] = False
            result["errors"].append(error)
            _err(error)

        if report.installed_agents:
            _ok("已检测到目标 Agent: " + ", ".join(report.installed_agents))
        else:
            error = "未检测到已安装的目标 Agent；8 Agent 满血分母未闭合"
            result["ok"] = False
            result["errors"].append(error)
            _err(error)

        if report.full_power_agents:
            _ok("满血 Agent: " + ", ".join(report.full_power_agents))

        if report.conformant_agents:
            _ok("静态合规 Agent: " + ", ".join(report.conformant_agents))

        for agent in report.agents:
            if not agent.installed or agent.full_power:
                continue
            result["ok"] = False
            error = f"{agent.name} 未达到满血接入标准（status={agent.status}）"
            result["errors"].append(error)
            _err(error)
            for gap in agent.full_power_gaps:
                print(f"    - conformance: {gap}")
            for gap in agent.runtime_gaps:
                print(f"    - runtime: {gap}")
            for action in agent.repair_actions:
                print(f"    repair: {action}")
        if not report.full_power_ok:
            result["ok"] = False
            error = (
                "8 Agent 满血分母未闭合: "
                + ", ".join(report.runtime_unverified_agents)
            )
            if error not in result["errors"]:
                result["errors"].append(error)
                _err(error)
    except VERIFY_OPERATION_ERRORS as e:
        result["ok"] = False
        result["errors"].append(str(e))
        _err(f"Agent Kit 满血验收异常: {e}")

    result["full_power_ok"] = bool(result["full_power_ok"] and result["ok"])
    return result


def run_verification(
    full: bool = False,
    api_smoke: bool = False,
    *,
    show_sensitive: bool = False,
    write_probes: bool = False,
) -> tuple[bool, dict]:
    """Run all default checks under one non-provisioning config snapshot."""
    from core.config import Config
    from core.ops.config_scope import use_config

    config = Config(provision=False)
    with use_config(config):
        return _run_verification_with_config(
            full=full,
            api_smoke=api_smoke,
            show_sensitive=show_sensitive,
            write_probes=write_probes,
        )


def _run_verification_with_config(
    full: bool = False,
    api_smoke: bool = False,
    *,
    show_sensitive: bool = False,
    write_probes: bool = False,
) -> tuple[bool, dict]:
    """Run verification checks and return the JSON payload."""
    print("=" * 60)
    print("Mnemos 部署验证")
    print("=" * 60)
    if show_sensitive:
        project_path = str(PROJECT_ROOT)
    else:
        from core.privacy.redaction import redact_path

        project_path = redact_path(PROJECT_ROOT)
    print(f"项目路径: {project_path}")

    results = {}
    all_ok = True

    _step("1. 环境检查")
    results["python"] = check_python_version()
    all_ok &= results["python"]

    _step("2. 代码编译检查")
    results["compileall"] = check_compileall()
    all_ok &= results["compileall"]

    _step("3. CLI / Daemon 可导入")
    results["cli"] = check_cli_help()
    results["daemon"] = check_daemon_import()
    all_ok &= results["cli"] and results["daemon"]

    _step("4. 目录权限")
    results["db_writable"] = (
        check_db_writable(write_probe=True) if write_probes else check_db_writable()
    )
    all_ok &= results["db_writable"]

    _step("5. Obsidian / Vault")
    obsidian_result = (
        check_obsidian_and_vaults(write_probe=True)
        if write_probes
        else check_obsidian_and_vaults()
    )
    results["obsidian_vaults"] = obsidian_result["ok"]
    all_ok &= obsidian_result["ok"]

    _step("6. LLM / Embedding / Reranker / 可选多模态配置")
    model_api_result = check_model_api_config(
        api_smoke=api_smoke,
        show_sensitive=show_sensitive,
    )
    results["model_apis"] = model_api_result["ok"]
    all_ok &= model_api_result["ok"]

    _step("7. Agent Kit 满血接入")
    agent_kit_result = check_agent_full_power()
    results["agent_kit_full_power"] = agent_kit_result["ok"]
    all_ok &= agent_kit_result["ok"]

    _step("8. Doctor 诊断")
    doctor_result = check_doctor()
    results["doctor"] = doctor_result["ok"]
    all_ok &= results["doctor"]

    _step("9. 核心链路集成测试")
    test_result = check_integration_tests(full=full)
    results["integration_tests"] = test_result["status"]
    if test_result["status"] != "skipped":
        all_ok &= test_result["ok"]
    full_verification_ok = bool(full and all_ok and test_result["ok"])

    # 总结
    print("\n" + "=" * 60)
    if full_verification_ok:
        print("✅ 完整验证通过 — Mnemos 已完成 full verification")
    elif all_ok:
        print("✅ 基础验证通过 — 集成测试未运行，不能标记为 full ready")
    elif full:
        print("❌ 完整验证未通过 — 请检查上方错误项")
    else:
        print("❌ 基础验证未通过 — 请检查上方错误项")
    print("=" * 60)

    payload = {
        "ok": all_ok,
        "verification_level": "full" if full else "basic",
        "full_verification_ok": full_verification_ok,
        "skipped_checks": ["integration_tests"] if test_result["status"] == "skipped" else [],
        "results": results,
        "obsidian": obsidian_result.get("obsidian", {}),
        "vaults": obsidian_result.get("vaults", {}),
        "obsidian_warnings": obsidian_result.get("warnings", []),
        "obsidian_errors": obsidian_result.get("errors", []),
        "doctor_warnings": doctor_result.get("warnings", []),
        "doctor_errors": doctor_result.get("errors", []),
        "model_apis": model_api_result.get("apis", {}),
        "model_api_warnings": model_api_result.get("warnings", []),
        "model_api_errors": model_api_result.get("errors", []),
        "agent_kit": agent_kit_result,
        "integration_tests": test_result,
        "tests_passed": test_result.get("passed", 0),
        "tests_failed": test_result.get("failed", 0),
    }
    if not show_sensitive:
        from core.privacy.redaction import redact_sensitive_data

        payload = redact_sensitive_data(payload)
    return all_ok, payload


def main():
    parser = argparse.ArgumentParser(description="Mnemos 部署后验证")
    parser.add_argument("--full", action="store_true", help="运行完整验证（含集成测试）")
    parser.add_argument(
        "--api-smoke",
        action="store_true",
        help="对已配置的 LLM/embedding/reranker/可选多模态运行真实 API smoke（会联网并消耗额度）",
    )
    parser.add_argument("--json", action="store_true", help="JSON 输出（用于脚本集成）")
    parser.add_argument(
        "--unsafe-debug",
        action="store_true",
        help="输出未脱敏的本机路径和端点，仅限本机排障",
    )
    parser.add_argument(
        "--write-probes",
        action="store_true",
        help="显式运行唯一文件写探针；默认验证只读且不创建目录",
    )
    args = parser.parse_args()

    if args.json:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            all_ok, payload = run_verification(
                full=args.full,
                api_smoke=args.api_smoke,
                show_sensitive=bool(args.unsafe_debug),
                write_probes=bool(args.write_probes),
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        all_ok, _ = run_verification(
            full=args.full,
            api_smoke=args.api_smoke,
            show_sensitive=bool(args.unsafe_debug),
            write_probes=bool(args.write_probes),
        )

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
