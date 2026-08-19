from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from core.kia.knowledge_inbox import KnowledgeInboxProcessor


class FakeStorageResult:
    uid = "storage:multimodal:1"


class FakeConfig:
    def __init__(self, root: Path) -> None:
        self.database_dir = root / "db"
        self.database_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.database_dir
        self.storage_backend = "obsidian"
        self._data = {
            "llm": {
                "provider_prices": {
                    "siliconflow": {
                        "vision-model": {"input": 0.1, "output": 0.2},
                        "Qwen/Qwen2.5-VL-72B-Instruct": {"input": 0.1, "output": 0.2},
                    }
                }
            },
            "model_call_ledger": {"daily_cost_cap": 100.0},
            "multimodal": {
                "enabled": False,
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key": "",
                "api_key_env": "MNEMOS_MULTIMODAL_API_KEY",
                "api_key_source": "",
                "model": "Qwen/Qwen2.5-VL-72B-Instruct",
                "max_input_tokens": 131072,
            }
        }

    def get(self, key: str, default=None):
        value = self._data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


def test_image_inbox_creates_recoverable_task_without_multimodal_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in (
        "MNEMOS_MULTIMODAL_API_KEY",
        "MNEMOS_MULTIMODAL_BASE_URL",
        "MNEMOS_MULTIMODAL_MODEL",
        "SILICONFLOW_API_KEY",
        "SILICONFLOW_MULTIMODAL_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = FakeConfig(tmp_path)

    with (
        patch("core.kia.knowledge_inbox.get_config", return_value=cfg),
        patch("core.kia.knowledge_inbox.create_storage_backend", return_value=MagicMock()),
        patch("core.kia.knowledge_inbox.DocumentProcessor", return_value=MagicMock()),
        patch("core.kia.knowledge_inbox.HEAT_TRACKER_AVAILABLE", False),
    ):
        processor = KnowledgeInboxProcessor()
        image = processor.inbox_dir / "screenshot.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        inbox_file = processor.scan_inbox()[0]
        result = processor.process_file(inbox_file)

    assert result["success"] is False
    assert result["recoverable"] is True
    assert result["multimodal_status"] == "skipped"
    assert "人工解析" in result["error"]
    task_path = Path(result["task_path"])
    assert task_path.exists()
    task = json.loads(task_path.read_text(encoding="utf-8"))
    assert task["schema_version"] == "mnemos.multimodal_image_task.v1"
    assert task["status"] == "needs_multimodal_config"
    assert task["recoverable"] is True
    assert any("MNEMOS_MULTIMODAL_API_KEY" in action for action in task["repair_actions"])
    assert not image.exists()
    assert Path(result["image_path"]).parent.name == ".manual"


def test_image_inbox_uses_configured_multimodal_api(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = FakeConfig(tmp_path)
    monkeypatch.setenv("MNEMOS_MULTIMODAL_API_KEY", "vision-key")
    monkeypatch.setenv("MNEMOS_MULTIMODAL_BASE_URL", "https://vision.example.test/v1")
    monkeypatch.setenv("MNEMOS_MULTIMODAL_MODEL", "vision-model")
    backend = MagicMock()
    backend.save.return_value = [FakeStorageResult()]
    response = MagicMock()
    response.headers = {"x-request-id": "request-multimodal-1"}
    response.json.return_value = {
        "id": "request-multimodal-1",
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        "choices": [{"message": {"content": "# Parsed image\n\nVisible text"}}]
    }
    dispatched_snapshots = []

    def fake_post(*_args, **_kwargs):
        with sqlite3.connect(str(cfg.database_dir / "model_call_ledger.db")) as conn:
            row = conn.execute(
                "SELECT lifecycle_state, request_dispatched, reserved_input_tokens "
                "FROM model_call_entries "
                "WHERE operation='multimodal_extract'"
            ).fetchone()
        dispatched_snapshots.append(row)
        return response

    with (
        patch("core.kia.knowledge_inbox.get_config", return_value=cfg),
        patch("core.kia.knowledge_inbox.create_storage_backend", return_value=backend),
        patch("core.kia.knowledge_inbox.DocumentProcessor", return_value=MagicMock()),
        patch("core.kia.knowledge_inbox.HEAT_TRACKER_AVAILABLE", False),
        patch("core.kia.amphora.enqueue_with_receipt") as enqueue,
        patch("requests.post", side_effect=fake_post) as post,
    ):
        processor = KnowledgeInboxProcessor()
        image = processor.inbox_dir / "screenshot.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        inbox_file = processor.scan_inbox()[0]
        result = processor.process_file(inbox_file)

    assert result["success"] is True
    assert result["recoverable"] is False
    assert result["multimodal_status"] == "processed"
    assert result["storage_uid"] == "storage:multimodal:1"
    assert Path(result["image_path"]).parent.name == ".processed"
    assert not image.exists()
    post.assert_called_once()
    payload = post.call_args.kwargs["json"]
    assert payload["model"] == "vision-model"
    assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    saved = backend.save.call_args.kwargs
    assert "Parsed image" in saved["content"]
    assert "source=multimodal" in saved["tags"]
    enqueue.assert_called()
    assert dispatched_snapshots == [("reserved", 1, 131072)]
    with sqlite3.connect(str(cfg.database_dir / "model_call_ledger.db")) as conn:
        ledger_row = conn.execute(
            "SELECT lifecycle_state, provider_usage_id, actual_input_tokens, actual_output_tokens "
            "FROM model_call_entries WHERE operation='multimodal_extract'"
        ).fetchone()
    assert ledger_row == ("settled", "", 5, 3)


def test_image_inbox_configured_multimodal_failure_is_recoverable(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    cfg = FakeConfig(tmp_path)
    monkeypatch.setenv("MNEMOS_MULTIMODAL_API_KEY", "vision-key")
    monkeypatch.setenv("MNEMOS_MULTIMODAL_BASE_URL", "https://vision.example.test/v1")
    monkeypatch.setenv("MNEMOS_MULTIMODAL_MODEL", "vision-model")

    marker = "RAW_PROVIDER_EXCEPTION_MARKER_multimodal"
    caplog.set_level(logging.WARNING)
    with (
        patch("core.kia.knowledge_inbox.get_config", return_value=cfg),
        patch("core.kia.knowledge_inbox.create_storage_backend", return_value=MagicMock()),
        patch("core.kia.knowledge_inbox.DocumentProcessor", return_value=MagicMock()),
        patch("core.kia.knowledge_inbox.HEAT_TRACKER_AVAILABLE", False),
        patch("requests.post", side_effect=requests.Timeout(marker)),
    ):
        processor = KnowledgeInboxProcessor()
        image = processor.inbox_dir / "screenshot.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        inbox_file = processor.scan_inbox()[0]
        result = processor.process_file(inbox_file)

    assert result["success"] is False
    assert result["recoverable"] is True
    assert result["multimodal_status"] == "unreachable"
    assert Path(result["image_path"]).parent.name == ".multimodal"
    task = json.loads(Path(result["task_path"]).read_text(encoding="utf-8"))
    assert task["status"] == "multimodal_processor_failed"
    assert task["error"] == "provider_timeout"
    assert marker not in result["error"]
    assert marker not in json.dumps(task)
    assert marker not in caplog.text
    assert "category=provider_timeout" in caplog.text
    with sqlite3.connect(str(cfg.database_dir / "model_call_ledger.db")) as conn:
        ledger_row = conn.execute(
            "SELECT lifecycle_state, request_dispatched, error_code "
            "FROM model_call_entries WHERE operation='multimodal_extract'"
        ).fetchone()
    assert ledger_row == ("incurred_unknown", 1, "multimodal_provider_exception")


def test_multimodal_rejects_missing_worst_case_allowance_before_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = FakeConfig(tmp_path)
    del cfg._data["multimodal"]["max_input_tokens"]
    api_cfg = SimpleNamespace(
        provider="siliconflow",
        api_key="vision-key",
        base_url="https://vision.example.test/v1",
        model="vision-model",
        timeout=1,
    )

    with (
        patch("core.kia.knowledge_inbox.get_config", return_value=cfg),
        patch("core.kia.knowledge_inbox.create_storage_backend", return_value=MagicMock()),
        patch("core.kia.knowledge_inbox.DocumentProcessor", return_value=MagicMock()),
        patch("core.kia.knowledge_inbox.HEAT_TRACKER_AVAILABLE", False),
        patch("requests.post") as post,
    ):
        processor = KnowledgeInboxProcessor()
        image = processor.inbox_dir / "unbounded.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        with pytest.raises(RuntimeError, match="multimodal.max_input_tokens"):
            processor._call_multimodal_api(api_cfg, image)

    post.assert_not_called()
    assert not (cfg.database_dir / "model_call_ledger.db").exists()
