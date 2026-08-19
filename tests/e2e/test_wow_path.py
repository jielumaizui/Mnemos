"""Wow-path e2e tests for the first-user value loop."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import e2e_wow_probe  # noqa: E402
from core.hephaestus.document_processor import DocumentProcessor  # noqa: E402


def test_wow_probe_mock_llm_report_closes_user_value_loop(tmp_path: Path) -> None:
    report = e2e_wow_probe.run_wow_probe(
        mode="mock_llm",
        root=tmp_path,
        emit=False,
    )

    assert report["ok"] is True
    assert report["mode"] == "mock_llm"
    assert report["user_intervention_count"] == 0
    assert report["final_value"]["wiki_page"]
    assert report["final_value"]["search_hits"] >= 1
    assert report["final_value"]["preflight_reminders"] >= 1

    steps = {step["id"]: step for step in report["steps"]}
    assert steps["config"].get("required_configured") == 3
    assert steps["multimodal"].get("status") == "skip"
    assert steps["document_import"].get("max_file_size_mb") == 100
    assert steps["document_import"].get("max_file_size_config_key") == (
        "document_process.max_file_size_mb"
    )
    assert steps["distill"].get("content_source") == "external_file"
    assert steps["distill"].get("intent_hypothesis") == "curate_or_decision_material"
    assert steps["wiki_route"].get("route_status") in {"routed", "needs_review"}
    assert steps["auto_heal"].get("mode") == "dry_run"

    wiki_page = Path(report["artifacts"]["wiki_page"])
    wiki_text = wiki_page.read_text(encoding="utf-8")
    assert "用户意图" in wiki_text
    assert "intent_evidence" in wiki_text or "意图证据" in wiki_text


def test_wow_probe_user_document_uses_supported_processor_type(tmp_path: Path) -> None:
    cfg = e2e_wow_probe.WowConfig(tmp_path.resolve())
    document_path = e2e_wow_probe._write_user_document(cfg)

    assert document_path.suffix in DocumentProcessor.SUPPORTED_EXTENSIONS


def test_wow_probe_dry_run_is_read_only(tmp_path: Path) -> None:
    report = e2e_wow_probe.run_wow_probe(
        mode="dry_run",
        root=tmp_path,
        emit=False,
    )

    assert report["ok"] is True
    assert report["mode"] == "dry_run"
    assert report["user_intervention_count"] == 0
    assert report["final_value"]["wiki_page"] == ""
    assert not (tmp_path / "wiki").exists()
    steps = {step["id"]: step for step in report["steps"]}
    assert steps["dry_run"].get("writes") == []
    assert steps["config"].get("required_configured") == 3


def test_wow_probe_cli_json_mock_llm(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/e2e_wow_probe.py",
            "--mock-llm",
            "--root",
            str(tmp_path),
            "--json",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == e2e_wow_probe.WOW_SCHEMA_VERSION
    assert payload["ok"] is True
