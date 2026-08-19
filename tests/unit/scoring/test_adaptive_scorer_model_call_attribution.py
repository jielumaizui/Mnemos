"""Provider-bound attribution for AdaptiveScorerV2 embedding features."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest


class RuntimeConfig:
    """Minimal runtime config with an isolated durable model-call ledger."""

    def __init__(self, root: Path) -> None:
        self.data_dir = root
        self.database_dir = root
        self.wiki_dir = root / "wiki"
        self._data = {
            "llm": {
                "provider_prices": {
                    "siliconflow": {
                        "embed-model": {"input": 0.1, "output": 0.2},
                    }
                }
            },
            "model_call_ledger": {"daily_cost_cap": 10.0},
        }

    def get(self, key: str, default=None):
        value = self._data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class _ScopeCapturingIndex:
    def __init__(self, seen_scopes: list[tuple[str, str] | None]) -> None:
        self._seen_scopes = seen_scopes

    def search(self, _query: str, **kwargs):
        self._seen_scopes.append(kwargs.get("subject_scope"))
        return []


def _subject_hash(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()


def _fixture_content(label: str) -> str:
    return "fixture-content-" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _scorer_with_provider(tmp_path, monkeypatch):
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient
    from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2

    config = RuntimeConfig(tmp_path)
    provider_calls: list[dict] = []
    client = SiliconFlowEmbeddingClient(
        api_key="embedding-key",
        base_url="https://embedding.example/v1",
        embedding_model="embed-model",
        config=config,
    )

    def create(**kwargs):
        provider_calls.append(kwargs)
        return SimpleNamespace(
            id="provider-request-id",
            usage=SimpleNamespace(total_tokens=7),
            data=[SimpleNamespace(embedding=[0.1, 0.2])],
        )

    fake_provider = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    monkeypatch.setattr(client, "_get_client", lambda: fake_provider)
    monkeypatch.setattr("core.scoring.adaptive_scorer_v2.get_config", lambda: config)
    monkeypatch.setattr(
        "core.embeddings.siliconflow_client.get_embedding_client", lambda: client
    )
    monkeypatch.setattr(AdaptiveScorerV2, "_load_all_models", lambda self: None)
    scorer = AdaptiveScorerV2(config={"backend": "lightweight"}, db_path=str(tmp_path / "scorer.db"))
    monkeypatch.setattr(scorer, "_should_compute_embedding", lambda _rule_confs: True)
    return scorer, config, provider_calls


def test_user_scoring_embedding_uses_user_scope_and_subject_delete_removes_ledger_entry(
    tmp_path, monkeypatch
):
    """The user content embedding and follow-up index query share one exact subject."""
    from core.telemetry.prompt_call_log import ModelCallLedger

    scorer, config, provider_calls = _scorer_with_provider(tmp_path, monkeypatch)
    seen_scopes: list[tuple[str, str] | None] = []
    monkeypatch.setattr(
        "core.embeddings.index_manager.EmbeddingIndexManager",
        lambda: _ScopeCapturingIndex(seen_scopes),
    )
    content = _fixture_content("scoped")

    scorer.score(
        {"content": content, "frontmatter": {}},
        dimensions=["sync"],
        subject_scope=("session", "subject-A"),
    )

    assert len(provider_calls) == 1
    assert seen_scopes == [("session", "subject-A")]

    ledger_path = config.database_dir / "model_call_ledger.db"
    with sqlite3.connect(ledger_path) as conn:
        entry_count = conn.execute(
            "SELECT COUNT(*) FROM model_call_entries WHERE operation='embedding'"
        ).fetchone()[0]
        bindings = conn.execute(
            "SELECT scope_kind, subject_hash FROM model_call_entry_subjects"
        ).fetchall()
    assert entry_count == 1
    assert ("session", _subject_hash("session", "subject-A")) in bindings
    assert content.encode("utf-8") not in ledger_path.read_bytes()

    ledger = ModelCallLedger.for_config(config)
    assert content not in json.dumps(ledger.recent(), sort_keys=True)
    ledger.freeze_subject_scope("session", "subject-A")
    deleted = ledger.delete_subject_scope("session", "subject-A")
    assert deleted["status"] == "applied"
    assert deleted["deleted_entry_count"] == 1
    with sqlite3.connect(ledger_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_call_entries").fetchone()[0] == 0


def test_unscoped_user_scoring_content_fails_closed_before_provider_dispatch(tmp_path, monkeypatch):
    """A bare content dict cannot be charged to the generic adaptive-scorer source."""
    scorer, config, provider_calls = _scorer_with_provider(tmp_path, monkeypatch)
    content = _fixture_content("unscoped")

    card = scorer.score({"content": content, "frontmatter": {}}, dimensions=["sync"])

    assert card.features["embedding_sim_to_high_quality"] is None
    assert provider_calls == []
    assert not (config.database_dir / "model_call_ledger.db").exists()


def test_generic_adaptive_scorer_scope_is_rejected_before_provider_dispatch(tmp_path, monkeypatch):
    """No caller may recreate the retired catch-all scope under a new API."""
    scorer, _config, provider_calls = _scorer_with_provider(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="generic adaptive scorer attribution is forbidden"):
        scorer.score(
            {"content": "content with no durable owner", "frontmatter": {}},
            dimensions=["sync"],
            subject_scope=("source", "adaptive_scorer"),
        )

    assert provider_calls == []


def test_explicit_system_owned_scope_is_allowed(tmp_path, monkeypatch):
    """System attribution is valid only when an owning caller supplies it explicitly."""
    scorer, config, provider_calls = _scorer_with_provider(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "core.embeddings.index_manager.EmbeddingIndexManager",
        lambda: _ScopeCapturingIndex([]),
    )

    scorer.score(
        {"content": "scheduled-system-artifact-content", "frontmatter": {}},
        dimensions=["sync"],
        subject_scope=("source", "scheduled-system-quality-scan"),
    )

    assert len(provider_calls) == 1
    with sqlite3.connect(config.database_dir / "model_call_ledger.db") as conn:
        assert conn.execute(
            "SELECT 1 FROM model_call_entry_subjects WHERE scope_kind=? AND subject_hash=?",
            ("source", _subject_hash("source", "scheduled-system-quality-scan")),
        ).fetchone() is not None
