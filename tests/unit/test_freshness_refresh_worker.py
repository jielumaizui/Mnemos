# -*- coding: utf-8 -*-
"""Tests for FreshnessRefreshWorker."""

from datetime import datetime, timedelta
import hashlib
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import sys

import pytest

from core.app.freshness_refresh_worker import FreshnessRefreshWorker
from core.telemetry.provider_request import canonical_chat_input, utf8_token_upper_bound
from core.trust.proposal_queue import ProposalQueue
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
    TrustedVaultMutationService,
)
from tests.cognitive_decision_fixtures import material_action_resolver


@pytest.fixture
def wiki(tmp_path):
    base = tmp_path / "wiki"
    base.mkdir()
    return base


def _write_page(wiki: Path, rel: str, fm: dict, body: str = ""):
    p = wiki / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = "\n".join(f"{k}: {v}" for k, v in fm.items())
    content = f"---\n{fm_lines}\n---\n\n{body}"
    p.write_text(content, encoding="utf-8")
    return p


def _authorized_worker(wiki: Path, **kwargs):
    return FreshnessRefreshWorker(
        wiki_base=str(wiki),
        material_action_resolver=material_action_resolver(
            wiki.parent,
            action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
            owner=TRUSTED_MARKDOWN_OWNER,
            executor=TRUSTED_MARKDOWN_EXECUTOR,
        ),
        **kwargs,
    )


def test_refresh_skips_timeless(wiki):
    _write_page(
        wiki,
        "04-Concepts/timeless.md",
        {"temporal_scope": "timeless", "updated_at": "2000-01-01"},
        "body",
    )
    worker = _authorized_worker(wiki)
    result = worker.refresh_page("04-Concepts/timeless.md")
    assert result.status == "skipped"


def test_refresh_updates_stale_page(wiki):
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    _write_page(wiki, "03-Tech/stale.md", {"updated_at": old}, "body")
    worker = _authorized_worker(wiki)
    result = worker.refresh_page("03-Tech/stale.md")
    assert result.status == "refreshed"
    assert result.backup_path.startswith("07-Shadow/08-Refresh/")
    content = (wiki / "03-Tech/stale.md").read_text(encoding="utf-8")
    assert "updated_at" in content
    today = datetime.now().strftime("%Y-%m-%d")
    assert "updated_at:" in content and today in content
    assert "修改日期:" in content and today in content


def test_refresh_enforce_submits_proposal_without_touching_page(wiki, monkeypatch):
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    page = _write_page(wiki, "03-Tech/stale.md", {"updated_at": old}, "body")
    original = page.read_text(encoding="utf-8")
    db = wiki / ".mnemos" / "trusted.db"
    fake_config = SimpleNamespace(
        wiki_dir=wiki,
        database_dir=wiki / ".mnemos",
        get=lambda key, default=None: {
            "trusted_push.mode": "enforce",
            "trusted_push.db_path": str(db),
        }.get(key, default),
    )
    monkeypatch.setattr("core.trust.config.get_config", lambda: fake_config)

    result = _authorized_worker(wiki).refresh_page("03-Tech/stale.md")

    assert result.status == "proposed"
    assert page.read_text(encoding="utf-8") == original
    proposals = ProposalQueue(db, wiki_base=wiki).list()
    assert proposals[0].candidate.source == "freshness_refresh"


def test_refresh_skips_fresh_page(wiki):
    today = datetime.now().strftime("%Y-%m-%d")
    _write_page(wiki, "03-Tech/fresh.md", {"updated_at": today}, "body")
    worker = _authorized_worker(wiki)
    result = worker.refresh_page("03-Tech/fresh.md")
    assert result.status == "skipped"


def test_refresh_all_stale_respects_limit(wiki):
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    for i in range(5):
        _write_page(wiki, f"03-Tech/stale{i}.md", {"updated_at": old}, "body")
    worker = _authorized_worker(wiki)
    report = worker.refresh_all_stale(limit=2)
    assert report["refreshed"] == 2
    assert report["scanned"] >= 2


def test_list_pages_filters_status(wiki):
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    _write_page(wiki, "stale.md", {"updated_at": old}, "body")
    _write_page(wiki, "fresh.md", {"updated_at": datetime.now().strftime("%Y-%m-%d")}, "body")
    worker = FreshnessRefreshWorker(wiki_base=str(wiki))
    stale = worker.list_pages(status_filter="stale")
    fresh = worker.list_pages(status_filter="fresh")
    assert len(stale) == 1
    assert stale[0]["status"] == "stale"
    assert len(fresh) == 1
    assert fresh[0]["status"] == "fresh"


# ========== 冷知识归档测试 ==========


def test_archive_cold_pages_moves_old_page(wiki):
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    _write_page(wiki, "03-Tech/cold.md", {"updated_at": old}, "body")
    worker = _authorized_worker(wiki)
    report = worker.archive_cold_pages(cutoff_days=30)
    assert report["archived"] == 1
    assert not (wiki / "03-Tech/cold.md").exists()
    archived = wiki / "99-Archive/Cold/03-Tech/cold.md"
    assert archived.exists()
    content = archived.read_text(encoding="utf-8")
    assert "status: archived" in content
    assert "archived_at:" in content


@pytest.mark.no_canonical_material_actions
def test_refresh_without_injected_authorization_seals_decisions(wiki):
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    page = _write_page(wiki, "03-Tech/project-contract.md", {"updated_at": old}, "body")

    result = FreshnessRefreshWorker(wiki_base=str(wiki)).refresh_page(
        "03-Tech/project-contract.md"
    )

    assert result.status == "refreshed"
    assert "updated_at" in page.read_text(encoding="utf-8")
    assert (wiki / "07-Shadow/08-Refresh/03-Tech/project-contract.md").is_file()
    state_db = (
        TrustedVaultMutationService(wiki_base=wiki).config.db_path.parent
        / "producer_consumer_ledger.db"
    )
    with sqlite3.connect(state_db) as conn:
        assert conn.execute(
            """
            SELECT COUNT(*)
            FROM cognitive_state_effect_receipts AS receipt
            JOIN cognitive_state_outbox AS command
              ON command.command_id=receipt.command_id
            WHERE receipt.status='committed'
              AND json_extract(command.payload_json, '$.target_ref') LIKE ?
            """,
            ("%project-contract.md",),
        ).fetchone() == (2,)


def test_archive_cold_pages_skips_fresh_page(wiki):
    today = datetime.now().strftime("%Y-%m-%d")
    _write_page(wiki, "03-Tech/fresh.md", {"updated_at": today}, "body")
    worker = FreshnessRefreshWorker(wiki_base=str(wiki))
    report = worker.archive_cold_pages(cutoff_days=30)
    assert report["archived"] == 0
    assert (wiki / "03-Tech/fresh.md").exists()


def test_archive_cold_pages_idempotent(wiki):
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    _write_page(wiki, "03-Tech/cold.md", {"updated_at": old, "status": "archived"}, "body")
    worker = FreshnessRefreshWorker(wiki_base=str(wiki))
    report = worker.archive_cold_pages(cutoff_days=30)
    assert report["archived"] == 0
    assert report["skipped"] == 1


def test_redistill_provider_reserves_before_dispatch_and_settles(wiki, tmp_path, monkeypatch):
    from core.llm_config import LLMApiChain, LLMApiConfig

    class RuntimeConfig:
        data_dir = tmp_path
        database_dir = tmp_path
        wiki_dir = wiki

        def get(self, key, default=None):
            if key == "llm.provider_prices":
                return {"test": {"test-model": {"input": 0.1, "output": 0.2}}}
            return default

    config = RuntimeConfig()
    chain = LLMApiChain(
        primary=LLMApiConfig(
            provider="test",
            api_key="test-key",
            base_url="https://provider.example/v1",
            model="test-model",
            source="test",
        )
    )
    snapshots = []
    constructor_kwargs = []
    captured_request = {}

    def create(**kwargs):
        captured_request.update(kwargs)
        with sqlite3.connect(str(tmp_path / "model_call_ledger.db")) as conn:
            snapshots.append(
                conn.execute(
                    "SELECT lifecycle_state, request_dispatched FROM model_call_entries "
                    "WHERE operation='freshness_redistill'"
                ).fetchone()
            )
            provider_input = canonical_chat_input(kwargs["messages"])
            reservation_input = conn.execute(
                "SELECT reserved_input_tokens, input_digest, reserved_output_tokens "
                "FROM model_call_entries WHERE operation='freshness_redistill'"
            ).fetchone()
        assert reservation_input == (
            utf8_token_upper_bound(provider_input),
            hashlib.sha256(provider_input.encode("utf-8")).hexdigest(),
            kwargs["max_tokens"],
        )
        return SimpleNamespace(
            id="request-freshness-1",
            usage=SimpleNamespace(prompt_tokens=9, completion_tokens=4),
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"body": "refreshed body"}'))],
        )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            constructor_kwargs.append(kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    monkeypatch.setattr("core.app.freshness_refresh_worker.get_config", lambda: config)
    monkeypatch.setattr("core.llm_config.resolve_llm_api_chain", lambda _config: chain)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    worker = FreshnessRefreshWorker(wiki_base=str(wiki), redistill_enabled=True)

    assert worker._redistill_body({"title": "Freshness"}, "secret 陈旧正文") == "refreshed body"
    assert snapshots == [("reserved", 1)]
    assert constructor_kwargs[0]["max_retries"] == 0
    assert captured_request["max_tokens"] == 6000
    assert captured_request["messages"][0] == {
        "role": "system",
        "content": "你是知识整理助手。只返回 JSON。",
    }
    with sqlite3.connect(str(tmp_path / "model_call_ledger.db")) as conn:
        row = conn.execute(
            "SELECT lifecycle_state, provider_usage_id, actual_input_tokens, actual_output_tokens "
            "FROM model_call_entries WHERE operation='freshness_redistill'"
        ).fetchone()
    assert row == ("settled", "", 9, 4)
    assert b"secret \xe9\x99\x88\xe6\x97\xa7\xe6\xad\xa3\xe6\x96\x87" not in (
        tmp_path / "model_call_ledger.db"
    ).read_bytes()


def test_redistill_empty_choices_preserves_dispatched_reservation(wiki, tmp_path, monkeypatch):
    from core.llm_config import LLMApiChain, LLMApiConfig

    class RuntimeConfig:
        data_dir = tmp_path
        database_dir = tmp_path
        wiki_dir = wiki

        def get(self, key, default=None):
            if key == "llm.provider_prices":
                return {"test": {"test-model": {"input": 0.1, "output": 0.2}}}
            return default

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        @staticmethod
        def _create(**_kwargs):
            return SimpleNamespace(
                id="freshness-empty-choices",
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1),
                choices=[],
            )

    chain = LLMApiChain(
        primary=LLMApiConfig(
            provider="test",
            api_key="test-key",
            base_url="https://provider.example/v1",
            model="test-model",
            source="test",
        )
    )
    monkeypatch.setattr(
        "core.app.freshness_refresh_worker.get_config", lambda: RuntimeConfig()
    )
    monkeypatch.setattr("core.llm_config.resolve_llm_api_chain", lambda _config: chain)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    worker = FreshnessRefreshWorker(wiki_base=str(wiki), redistill_enabled=True)

    assert worker._redistill_body({"title": "Freshness"}, "secret 空选择") == "secret 空选择"
    with sqlite3.connect(str(tmp_path / "model_call_ledger.db")) as conn:
        row = conn.execute(
            "SELECT lifecycle_state, request_dispatched, error_code "
            "FROM model_call_entries WHERE operation='freshness_redistill'"
        ).fetchone()
    # The provider supplied an explicit usage receipt before its malformed
    # response was indexed, so settlement is the accurate terminal state.
    assert row == ("settled", 1, "")
