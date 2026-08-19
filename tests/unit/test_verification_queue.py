import json
import sqlite3
from pathlib import Path

import mnemos_cli
from core.cognitive.verification_queue import VerificationQueue


class FakeConfig:
    def __init__(self, tmp_path: Path):
        self.database_dir = tmp_path
        self.wiki_dir = tmp_path / "wiki"
        self.values = {
            "verification_queue.enabled": True,
            "verification_queue.blindspots_db_path": None,
            "verification_queue.max_candidates": 50,
            "verification_queue.max_disputes": 10,
            "verification_queue.max_blindspots": 10,
            "verification_queue.max_freshness_alerts": 10,
            "verification_queue.respect_resource_budget": True,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


def _source_rows():
    return {
        "disputes": [
            {
                "path": "08-Disputes/redis-conflict.md",
                "title": "redis conflict",
                "days_old": 8,
                "needs_escalation": True,
            }
        ],
        "blindspots": [
            {
                "topic": "vector clocks",
                "description": "用户多次提到但知识库缺少方法论",
                "confidence": 0.82,
                "status": "detected",
                "detected_at": "2026-07-03T00:00:00",
            }
        ],
        "freshness": [
            {
                "path": "03-Tech/openai-api.md",
                "status": "stale",
                "severity": "high",
                "message": "版本信息超过 freshness 阈值",
            }
        ],
    }


def test_plan_consumes_dispute_blindspot_and_freshness_with_evidence(tmp_path):
    queue = VerificationQueue(config=FakeConfig(tmp_path))

    report = queue.plan(source_rows=_source_rows())

    assert report["schema_version"] == "mnemos.verification_report.v1"
    assert report["task_count"] == 3
    assert report["counts"] == {"dispute": 1, "blindspot": 1, "freshness": 1}
    assert report["conclusions_have_evidence"] is True
    assert report["writes"]["wiki_body"] is False
    for task in report["tasks"]:
        assert task["evidence_refs"] or task["verification_commands"]
        assert task["conclusion"]


def test_apply_writes_queue_and_report_without_modifying_wiki_body(tmp_path):
    cfg = FakeConfig(tmp_path)
    cfg.wiki_dir.mkdir()
    page = cfg.wiki_dir / "03-Tech"
    page.mkdir()
    wiki_page = page / "openai-api.md"
    original = "# OpenAI API\n\nold content"
    wiki_page.write_text(original, encoding="utf-8")

    queue = VerificationQueue(config=cfg)
    report = queue.run(apply=True, source_rows=_source_rows())

    assert report["status"] == "ok"
    assert report["writes"] == {
        "verification_db": True,
        "report": True,
        "wiki_body": False,
        "code": False,
    }
    assert wiki_page.read_text(encoding="utf-8") == original
    assert Path(report["report_path"]).exists()
    with sqlite3.connect(str(Path(report["db_path"]))) as conn:
        count = conn.execute("SELECT COUNT(*) FROM verification_queue").fetchone()[0]
        run_count = conn.execute("SELECT COUNT(*) FROM verification_runs").fetchone()[0]
    assert count == 3
    assert run_count == 1


def test_background_run_defers_when_resource_budget_blocks(tmp_path):
    class BlockingBudget:
        def can_run(self, service):
            assert service == "verification_queue"
            return False

        def throttle_delay(self, service):
            assert service == "verification_queue"
            return 15

        def status(self):
            return {"state": "battery", "cpu": "10.0%", "memory": "20.0%"}

    queue = VerificationQueue(config=FakeConfig(tmp_path))
    report = queue.run(apply=True, background=True, budget=BlockingBudget())

    assert report["status"] == "deferred"
    assert report["reason"] == "resource_budget"
    assert report["retry_after_seconds"] == 15
    assert report["writes"]["verification_db"] is False
    assert not (tmp_path / "verification_queue.db").exists()


def test_json_report_is_serializable(tmp_path):
    queue = VerificationQueue(config=FakeConfig(tmp_path))
    report = queue.plan(source_rows=_source_rows(), limit=2)

    encoded = json.dumps(report, ensure_ascii=False)

    assert "mnemos.verification_report.v1" in encoded
    assert report["task_count"] == 2


def test_cli_parser_registers_verify_run_command():
    parser = mnemos_cli.build_parser()

    args = parser.parse_args(["verify", "run", "--json", "--limit", "2"])

    assert args.command == "verify"
    assert args.verify_cmd == "run"
    assert args.json is True
    assert args.limit == 2
