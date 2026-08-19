"""
KIAEventConsumer 单元测试

验证 immune.report / dna.computed / entropy.suggestions 三个事件
能被消费者正确持久化为 Wiki 报告、DB 记录与 frontmatter 更新。
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.kia.kia_event_consumer import (
    KIAEventConsumer,
    compact_entropy_report_frontmatter,
)


@pytest.fixture(autouse=True)
def _isolated_trusted_config(tmp_path, monkeypatch):
    """Bind formal KIA writes to the per-test state directory."""

    config = SimpleNamespace(
        database_dir=tmp_path,
        wiki_dir=tmp_path,
        get=lambda _key, default=None: default,
    )
    monkeypatch.setattr("core.trust.config.get_config", lambda: config)
    yield


@pytest.fixture
def consumer(tmp_path):
    """提供使用临时 Wiki 目录的 KIAEventConsumer。"""
    return KIAEventConsumer(wiki_base=str(tmp_path))


class TestImmuneReportConsumer:
    """immune.report 事件消费者测试。"""

    def test_on_immune_report_writes_markdown_report(self, consumer, tmp_path):
        payload = {
            "scanned_pages": 10,
            "issue_count": 2,
            "critical_count": 1,
            "health_score": 85,
            "issues": [
                {
                    "issue_type": "conflict",
                    "severity": "critical",
                    "page": "/wiki/a.md",
                    "description": "矛盾关系",
                    "suggestion": "检查边界",
                },
                {
                    "issue_type": "orphan",
                    "severity": "medium",
                    "page": "/wiki/b.md",
                    "description": "孤立页面",
                    "suggestion": "建立关系",
                },
            ],
        }

        result = consumer.on_immune_report(payload)

        assert result["status"] == "ok"
        assert result["issue_count"] == 2
        assert result["critical_count"] == 1
        report_path = Path(result["report_path"])
        assert report_path.exists()
        text = report_path.read_text(encoding="utf-8")
        assert "知识免疫报告" in text
        assert "矛盾关系" in text
        assert "孤立页面" in text
        assert "source_count: 1" in text
        assert "event:immune.report:" in text

    def test_on_immune_report_empty_issues(self, consumer, tmp_path):
        payload = {
            "scanned_pages": 5,
            "issue_count": 0,
            "critical_count": 0,
            "health_score": 100,
            "issues": [],
        }

        result = consumer.on_immune_report(payload)

        assert result["status"] == "ok"
        report_path = Path(result["report_path"])
        assert report_path.exists()
        assert "问题清单" not in report_path.read_text(encoding="utf-8")


class TestDNAComputedConsumer:
    """dna.computed 事件消费者测试。"""

    def test_on_dna_computed_saves_dna_and_updates_frontmatter(self, consumer, tmp_path):
        page = tmp_path / "test-page.md"
        page.write_text("---\ntype: note\n---\n\n# Test\n", encoding="utf-8")

        payload = {
            "page_path": str(page),
            "content_md5": "md5-abc",
            "content_simhash": "simhash-abc",
            "semantic_signature": "tech:concept:入门:中性",
            "domain_type_hash": "hash-abc",
            "domain": "tech",
            "knowledge_type": "concept",
            "complexity": "入门",
            "emotion": "中性",
            "keyword_set": ["k1", "k2"],
            "core_concepts": ["c1"],
            "scenario_tags": [],
            "tool_entities": [],
            "title_keywords": ["t1"],
            "title_pattern": "guide",
            "confidence": 0.8,
            "evidence_level": "single-source",
            "temporal": "contextual",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }

        fake_engine = MagicMock()
        fake_engine.save_dna.return_value = True

        with patch("core.kia.genos.DNAEngine", return_value=fake_engine):
            result = consumer.on_dna_computed(payload)

        assert result["status"] == "ok"
        assert result["dna_hash"] == "simhash-abc"
        assert fake_engine.save_dna.call_count == 1
        saved_dna = fake_engine.save_dna.call_args[0][0]
        assert saved_dna.page_path == str(page)
        assert saved_dna.content_simhash == "simhash-abc"

        updated = page.read_text(encoding="utf-8")
        assert "dna_hash: simhash-abc" in updated
        assert "dna_domain: tech" in updated
        assert "dna_type: concept" in updated

    def test_on_dna_computed_skips_missing_page_path(self, consumer):
        result = consumer.on_dna_computed({})
        assert result["status"] == "skipped"
        assert result["reason"] == "no page_path"


class TestEntropySuggestionsConsumer:
    """entropy.suggestions 事件消费者测试。"""

    def test_entropy_frontmatter_provenance_is_bounded_for_large_batches(self):
        compacted = compact_entropy_report_frontmatter(
            {
                "report_type": "entropy_suggestions",
                "source_db": "/tmp/entropy.db",
                "source_payload_digest": "a" * 64,
                "source_row_ids": list(range(1, 5001)),
                "sources": [
                    f"sqlite:/tmp/entropy.db#entropy_suggestions/{i}" for i in range(1, 5001)
                ],
            }
        )

        assert "source_row_ids" not in compacted
        assert "sources" not in compacted
        assert compacted["source_row_id_first"] == 1
        assert compacted["source_row_id_last"] == 5000
        assert compacted["source_row_id_count"] == 5000
        assert compacted["source_row_ids_hash"].startswith("sha256:")
        assert len(str(compacted).encode("utf-8")) < 4096

    def test_on_entropy_suggestions_persists_and_reports(self, consumer, tmp_path):
        payload = {
            "trigger": "incremental",
            "estimated_savings": {"chars": 1000},
            "candidates": [
                {
                    "page_a": "/wiki/a.md",
                    "page_b": "/wiki/b.md",
                    "similarity": 0.95,
                    "merge_strategy": "delete_duplicate",
                    "reason": "完全重复",
                    "recommended_action": "删除 b.md",
                    "confidence": 0.9,
                }
            ],
        }

        result = consumer.on_entropy_suggestions(payload)

        assert result["status"] == "ok"
        assert result["candidate_count"] == 1

        db_path = tmp_path / ".kg" / "entropy_suggestions.db"
        assert db_path.exists()
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("SELECT * FROM entropy_suggestions").fetchall()
            assert len(rows) == 1
            assert rows[0][3] == "/wiki/a.md"
            assert rows[0][4] == "/wiki/b.md"

        report_files = list((tmp_path / "06-Retrospectives" / "entropy").glob("*.md"))
        assert len(report_files) == 1
        text = report_files[0].read_text(encoding="utf-8")
        assert "熵减建议报告" in text
        assert "完全重复" in text
        assert "source_count: 1" in text
        frontmatter = text.split("---", 2)[1]
        assert "source_row_ids:" not in frontmatter
        assert "sources:" not in frontmatter
        assert "source_row_id_count: 1" in frontmatter
        assert "entropy_suggestions?source_digest=" in frontmatter

    def test_on_entropy_suggestions_empty_candidates(self, consumer, tmp_path):
        payload = {"trigger": "scan", "estimated_savings": {}, "candidates": []}

        result = consumer.on_entropy_suggestions(payload)

        assert result["status"] == "ok"
        assert result["candidate_count"] == 0
        db_path = tmp_path / ".kg" / "entropy_suggestions.db"
        assert db_path.exists()
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("SELECT * FROM entropy_suggestions").fetchall()
            assert len(rows) == 0
        report_files = list((tmp_path / "06-Retrospectives" / "entropy").glob("*.md"))
        assert len(report_files) == 0
