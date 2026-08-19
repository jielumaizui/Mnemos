from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import zlib


def _seed_profile_store(tmp_path: Path):
    from core.application.persona import PersonaApplicationService
    from core.evidence.source_authority import SourceAuthorityCatalog
    from core.persona.psyche import SignalStore

    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    quote = "用户希望每个问题按修复、测试、文档同步、本地提交的闭环处理。"
    revision_id = "raw-revision-integration-profile-v2"
    content_hash = "sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest()
    raw_db = tmp_path / "raw_events.db"
    with sqlite3.connect(raw_db) as conn:
        conn.execute("""
            CREATE TABLE raw_turn_revisions (
                revision_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                snapshot_blob BLOB NOT NULL
            )
            """)
        conn.execute(
            "INSERT INTO raw_turn_revisions VALUES (?, ?, ?)",
            (
                revision_id,
                content_hash,
                zlib.compress(
                    json.dumps(
                        {"user_content": quote},
                        ensure_ascii=False,
                    ).encode("utf-8")
                ),
            ),
        )
    catalog = SourceAuthorityCatalog.from_messages(
        (
            {
                "role": "user",
                "content": quote,
                "source_span": {
                    "revision_id": revision_id,
                    "span_start": 0,
                    "span_end": len(quote),
                    "role": "user",
                    "content_hash": content_hash,
                },
            },
        ),
        allowed_source_event_ids=(revision_id,),
    )
    entry = catalog.entries[0]
    result = PersonaApplicationService().record_explicit_profile_evidence(
        source_authority_catalog=catalog,
        source_authority_id=entry.source_authority_id,
        raw_db_path=raw_db,
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
        signal_type="explicit_preference",
        dimension="interaction_contract",
        quote=quote,
        confidence=0.92,
        signal_store=store,
    )
    assert result["signal_id"] == 1
    assert result["assertion_id"]
    return store


def _profile_principal():
    from core.access_policy import PrincipalEnvelope

    return PrincipalEnvelope(
        principal_id="mcp:codex:profile-loop",
        agent="codex",
        host_kind="codex",
        capability_id="profile-loop",
        capabilities=frozenset({"memory_read"}),
    )


def _profile_narrowing():
    from core.access_policy import AccessNarrowing

    return AccessNarrowing(session_id="profile-session")


def test_profile_signal_assertion_usage_loop(monkeypatch, tmp_path: Path) -> None:
    from core.app.context_search import ContextAwareSearch
    from core.application.intelligence import IntelligenceApplicationService
    from core.hephaestus.prompt_builder import ContextAssembler, DistillTask, Session
    from integrations import preflight_builder

    store = _seed_profile_store(tmp_path)

    class FakeConfig:
        database_dir = tmp_path
        wiki_dir = tmp_path
        data_dir = tmp_path
        persona_enabled = True

        def get(self, key, default=None):
            values = {
                "persona.enabled": self.persona_enabled,
                "persona.strategy_injection_enabled": True,
                "persona.strategy_token_limit": 512,
            }
            return values.get(key, default)

    fake_config = FakeConfig()
    monkeypatch.setattr("core.persona.psyche.get_signal_store", lambda: store)
    monkeypatch.setattr("core.app.context_search.get_config", lambda: fake_config)
    monkeypatch.setattr("core.config.get_config", lambda: fake_config)
    monkeypatch.setattr("integrations.preflight_builder.get_config", lambda: fake_config)
    monkeypatch.setattr(
        "integrations.preflight_builder._load_contextual_persona_profiles",
        lambda _principal, _narrowing: ({}, {}),
    )
    monkeypatch.setattr("core.persona.delphi.get_behavior_prompt", lambda _agent: "")

    fake_config.persona_enabled = False
    disabled_preflight = preflight_builder.build_persona_section(
        "codex",
        working_dir=str(tmp_path),
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
    )
    disabled_usage_count = (
        store._pool.get_conn().execute("SELECT COUNT(*) FROM profile_usage_log").fetchone()[0]
    )
    fake_config.persona_enabled = True
    preflight_section = preflight_builder.build_persona_section(
        "codex",
        working_dir=str(tmp_path),
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
    )
    page = tmp_path / "profile-loop.md"
    page.write_text(
        "---\n"
        "title: 问题清单闭环修复\n"
        "scope: agent\n"
        "source_agent: codex\n"
        "acl_schema_version: 1\n"
        "acl_metadata_complete: true\n"
        "acl_reconciliation_status: proven\n"
        "---\n"
        "问题清单闭环修复需要保持证据、测试和提交一致。\n",
        encoding="utf-8",
    )
    searcher = ContextAwareSearch(wiki_base=str(tmp_path))
    monkeypatch.setattr(
        "core.app.context_search.ContextAwareSearch",
        lambda: searcher,
    )
    search_response = IntelligenceApplicationService().context_aware_search(
        "问题清单闭环修复",
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
    )
    weights = searcher._get_profile_weights(
        _profile_principal(),
        _profile_narrowing(),
    )
    distill_context = ContextAssembler(tmp_path).assemble(
        DistillTask(
            task_type="extract",
            session=Session(
                id="profile-v2-loop",
                agent_name="codex",
                messages=[{"role": "user", "content": "继续按问题清单闭环修复"}],
            ),
        )
    )

    assert disabled_preflight == ""
    assert disabled_usage_count == 0
    assert "User Cognitive Profile v2" in preflight_section
    assert "修复、测试、文档同步、本地提交" in preflight_section
    assert search_response["count"] == 1
    assert weights["persona_assertions"]
    assert distill_context["cognitive_profile_context"].endswith("- none")

    metrics = store.get_authorized_profile_usage_metrics(
        days=7,
        principal=_profile_principal(),
        narrowing=_profile_narrowing(),
        purpose="persona_usage_metrics",
    )
    # A single matched result does not change top-k order, so Context Search
    # must not manufacture a ranking-effect receipt.
    assert metrics["total_usages"] == 1
    assert metrics["action_changed_count"] == 1
    assert metrics["by_consumer"] == {"preflight_builder": 1}
    usage = store._pool.get_conn().execute("""
        SELECT action_changed, baseline_hash, persona_enabled_hash,
               terminal_status, actual_target_delta
        FROM profile_usage_log
        """).fetchone()
    assert tuple(usage[:4]) == (
        1,
        usage[1],
        usage[2],
        "committed",
    )
    assert usage[1] != usage[2]
    target_delta = json.loads(str(usage[4]))
    assert target_delta["changed"] is True
    assert target_delta["target_id"] == "preflight_persona_section"
