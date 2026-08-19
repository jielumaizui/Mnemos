import json
import logging
import sqlite3
from types import SimpleNamespace

from core.hephaestus.distillation_failure import (
    record_distillation_failure,
    save_failed_distill,
)
from core.ops.operational_incident import OperationalIncidentStore
from core.hephaestus.distillation_json import (
    JSON_PARSE_FAILED,
    JSON_PARSE_FIXED,
    JSON_PARSE_MARKDOWN,
    extract_json_with_metadata,
)
from core.hephaestus.distillation_llm import HttpApiHostAgentCaller
from core.hephaestus.distillation_metrics import (
    record_json_parse_event,
    summarize_json_parse_metrics,
)
from core.llm_config import LLMApiChain, LLMApiConfig


def _fake_config(tmp_path):
    return SimpleNamespace(
        database_dir=tmp_path,
        wiki_dir=tmp_path / "wiki",
        get=lambda _key, default=None: default,
    )


def _api_chain() -> LLMApiChain:
    return LLMApiChain(
        primary=LLMApiConfig(
            provider="test",
            api_key="key",
            base_url="https://example.test/v1",
            model="json-model",
            source="test",
        )
    )


def test_extract_json_markdown_fallback_does_not_emit_warning(caplog):
    caplog.set_level(logging.WARNING, logger="core.hephaestus.distillation_json")

    result = extract_json_with_metadata('```json\n{"judgment": "knowledge"}\n```')

    assert result.success is True
    assert result.path == JSON_PARSE_MARKDOWN
    assert result.fallback_used is True
    assert result.data == {"judgment": "knowledge"}
    assert not [
        record for record in caplog.records if "JSON extraction failed" in record.message
    ]


def test_extract_json_final_failure_records_structured_metadata(caplog):
    caplog.set_level(logging.WARNING, logger="core.hephaestus.distillation_json")

    result = extract_json_with_metadata("not json at all")

    assert result.success is False
    assert result.path == JSON_PARSE_FAILED
    payload = result.as_dict()
    assert payload["error_class"] in {"JSONDecodeError", "EmptyResponse"}
    assert payload["correction_attempts"] >= 1
    assert any("JSON extraction failed" in record.message for record in caplog.records)


def test_distill_json_parse_metrics_summarize_paths(tmp_path):
    direct = extract_json_with_metadata('{"ok": true}')
    markdown = extract_json_with_metadata('```json\n{"ok": true}\n```')
    fixed = extract_json_with_metadata('{"ok": true,}')
    failed = extract_json_with_metadata("not json")

    for result in (direct, markdown, fixed, failed):
        record_json_parse_event(tmp_path, result, provider="test", model="json-model")

    summary = summarize_json_parse_metrics(tmp_path, warning_failure_rate=0.2)

    assert summary["schema_version"] == "mnemos.distill_json_quality.v1"
    assert summary["status"] == "warning"
    assert summary["total_events"] == 4
    assert summary["success"] == 3
    assert summary["failed"] == 1
    assert summary["fallback_success"] == 2
    assert summary["fixed_json_success"] == 1
    assert summary["by_parse_path"][JSON_PARSE_FIXED] == 1
    assert summary["rates"]["final_failure_rate"] == 0.25


def test_llm_caller_records_json_parse_metadata(tmp_path, monkeypatch):
    cfg = _fake_config(tmp_path)
    caller = HttpApiHostAgentCaller(
        api_chain=_api_chain(),
        config_getter=lambda: cfg,
        wiki_db_getter=lambda: tmp_path / "wiki_state.db",
    )

    def fake_try(_prompt, _timeout, _cfg, max_tokens=None):
        return '```json\n{"result": "fallback"}\n```', {"prompt_tokens": 1, "completion_tokens": 2, "cost": 0.0}

    monkeypatch.setattr(caller, "_try_api_config", fake_try)

    result = caller.call("prompt", expect_json=True)

    assert result == {"result": "fallback"}
    assert caller.last_usage["json_parse"]["path"] == JSON_PARSE_MARKDOWN
    with sqlite3.connect(str(tmp_path / "distill_metrics.db")) as conn:
        row = conn.execute(
            "SELECT parse_path, success FROM distill_json_parse_events"
        ).fetchone()
    assert row == (JSON_PARSE_MARKDOWN, 1)


def test_failed_distill_file_contains_structured_error_metadata(tmp_path):
    path = save_failed_distill(
        session_id="session-1",
        fragments=[],
        validation_errors=["fragment[0].title too short"],
        database_dir=tmp_path,
        source="unit",
        raw_response='{"bad": true}',
        parse_metadata={"path": JSON_PARSE_FIXED, "correction_attempts": 2},
    )

    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["failure_class"] == "distill_validation"
    assert data["error_fingerprint"]
    assert data["parse_metadata"]["path"] == JSON_PARSE_FIXED
    assert data["parse_metadata"]["correction_attempts"] == 2
    assert data["raw_output"]["stored_in"] == str(path)
    assert data["raw_output"]["available"] is True


def test_distill_failure_incident_clusters_same_error(tmp_path):
    from core.ops.operational_incident import initialize_operational_incident_schema

    initialize_operational_incident_schema(tmp_path / "operational_incidents.db")
    errors = ["fragment[0].frontmatter.摘要 missing"]
    first = record_distillation_failure(
        "session-a",
        [],
        errors,
        tmp_path,
        source="unit",
    )
    second = record_distillation_failure(
        "session-b",
        [],
        errors,
        tmp_path,
        source="unit",
    )

    assert first.incident.incident_id == second.incident.incident_id
    store = OperationalIncidentStore(tmp_path / "operational_incidents.db")
    assert len(store.list_occurrences(first.incident.incident_id)) == 2
    assert not (tmp_path / "recap_tasks.db").exists()
