from __future__ import annotations

from pathlib import Path

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.app.raw_search import RawIndex
from core.application.storage import StorageApplicationService
from core.sync_framework.raw_event_store import RawEventStore


class _Cfg:
    def __init__(self, root: Path):
        self.database_dir = root / "db"
        self.obsidian_vault_path = root / "raw"
        self.database_dir.mkdir()
        self.obsidian_vault_path.mkdir()

    def get(self, key, default=None):
        values = {
            "raw_event_store.db_path": str(self.database_dir / "raw_events.db"),
            "raw_event_store.enabled": True,
        }
        return values.get(key, default)


def _principal(agent: str) -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id=f"test:{agent}",
        agent=agent,
        host_kind="test",
        capability_id="test-capability",
        capabilities=frozenset({"memory_read"}),
    )


def _build_fixture(tmp_path: Path):
    cfg = _Cfg(tmp_path)
    sentinel = "CANONICAL-SENTINEL-BEYOND-PROJECTION"
    store = RawEventStore(config=cfg)
    revision_id = store.upsert_turn(
        source_agent="codex",
        session_id="session-canonical",
        turn_number=0,
        user_content=("prefix " * 2200) + sentinel,
        assistant_content="full canonical answer",
        completeness={"visible_text": "full"},
    )

    projection = cfg.obsidian_vault_path / "stale.md"
    projection.write_text(
        "---\n"
        "session_id: session-canonical\n"
        "source: codex\n"
        "scope: private\n"
        "acl_schema_version: 1\n"
        "acl_metadata_complete: true\n"
        "acl_reconciliation_status: canonical_raw_index\n"
        "---\n"
        + ("prefix " * 100),
        encoding="utf-8",
    )
    index = RawIndex(
        raw_dir=cfg.obsidian_vault_path,
        db_path=cfg.database_dir / "raw_index.db",
        config=cfg,
        raw_event_store=store,
    )
    index.sync_index()
    index.close()
    store.close()
    return cfg, revision_id, sentinel


def test_search_fetches_canonical_revision_when_projection_is_truncated(
    tmp_path, monkeypatch
):
    cfg, revision_id, sentinel = _build_fixture(tmp_path)
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    service = StorageApplicationService(lambda: None)

    result = service.session_search(
        query=sentinel,
        session_id="session-canonical",
        principal=_principal("codex"),
        narrowing=AccessNarrowing(session_id="session-canonical"),
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["evidence_source"] == "raw_event_revision"
    assert result["results"][0]["revision_id"] == revision_id
    assert result["results"][0]["evidence_source"] == "raw_event_revision"
    assert sentinel in result["results"][0]["snippet"]
    assert result["results"][0]["projection_candidate"] is False


def test_search_survives_deleted_projection_and_denies_before_body_read(
    tmp_path, monkeypatch
):
    cfg, revision_id, sentinel = _build_fixture(tmp_path)
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    (cfg.database_dir / "raw_index.db").unlink()
    service = StorageApplicationService(lambda: None)

    allowed = service.session_search(
        query=sentinel,
        session_id="session-canonical",
        principal=_principal("codex"),
        narrowing=AccessNarrowing(session_id="session-canonical"),
    )
    assert [item["revision_id"] for item in allowed["results"]] == [revision_id]

    def forbidden_body_read(*_args, **_kwargs):
        raise AssertionError("unauthorized canonical body was read")

    monkeypatch.setattr(RawEventStore, "get_turn", forbidden_body_read)
    denied = service.session_search(
        query=sentinel,
        session_id="session-canonical",
        principal=_principal("claude"),
        narrowing=AccessNarrowing(session_id="session-canonical"),
    )
    assert denied["success"] is True
    assert denied["results"] == []
    assert denied["access_filter"]["private_cross_agent_denied"] == 1


def test_revision_uid_round_trips_after_the_logical_turn_is_superseded(
    tmp_path, monkeypatch
):
    cfg = _Cfg(tmp_path)
    store = RawEventStore(config=cfg)
    old_revision = store.upsert_turn(
        source_agent="codex",
        session_id="session-revised",
        turn_number=0,
        user_content="OLD-REVISION-SENTINEL",
        assistant_content="old answer",
    )
    new_revision = store.upsert_turn(
        source_agent="codex",
        session_id="session-revised",
        turn_number=0,
        user_content="new content",
        assistant_content="new answer",
    )
    store.close()
    assert old_revision != new_revision
    monkeypatch.setattr("core.config.get_config", lambda: cfg)

    result = StorageApplicationService(lambda: None).session_search(
        uid=f"raw-revision:{old_revision}",
        query="OLD-REVISION-SENTINEL",
        principal=_principal("codex"),
        narrowing=AccessNarrowing(),
    )

    assert result["success"] is True
    assert [item["revision_id"] for item in result["results"]] == [old_revision]
    assert "OLD-REVISION-SENTINEL" in result["results"][0]["snippet"]


def test_legacy_projection_uid_fails_closed_without_reading_projection_body(
    tmp_path, monkeypatch
):
    cfg, _revision_id, _sentinel = _build_fixture(tmp_path)
    monkeypatch.setattr("core.config.get_config", lambda: cfg)

    class ForbiddenBackend:
        def get_by_id(self, _uid):
            raise AssertionError("projection body must not be read before authorization")

    result = StorageApplicationService(lambda: ForbiddenBackend()).session_search(
        uid="stale.md",
        principal=_principal("codex"),
        narrowing=AccessNarrowing(),
    )

    assert result == {
        "success": False,
        "message": "uid is not a canonical Raw event or revision",
        "uid_resolution": "metadata_only_fail_closed",
    }


def test_logical_raw_event_uid_authorizes_header_before_body_read(tmp_path, monkeypatch):
    cfg, revision_id, sentinel = _build_fixture(tmp_path)
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    store = RawEventStore(config=cfg)
    logical_event_id = store.get_logical_event_id(revision_id)
    store.close()

    result = StorageApplicationService(lambda: None).session_search(
        uid=f"raw-event:{logical_event_id}",
        query=sentinel,
        principal=_principal("codex"),
        narrowing=AccessNarrowing(),
    )

    assert result["success"] is True
    assert [item["revision_id"] for item in result["results"]] == [revision_id]


def test_canonical_snippet_keeps_match_after_large_neighbor_lines():
    sentinel = "MATCH-MUST-SURVIVE"
    text = ("a" * 4000) + "\n" + sentinel + "\n" + ("b" * 4000)

    snippet, matched_line, line_number = StorageApplicationService._canonical_snippet(
        text, sentinel.casefold()
    )

    assert sentinel in snippet
    assert matched_line == sentinel
    assert line_number == 2
