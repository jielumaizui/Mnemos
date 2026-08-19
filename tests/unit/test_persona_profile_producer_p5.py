from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from pathlib import Path

import pytest


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_raw_revision(
    raw_db: Path,
    *,
    revision_id: str,
    user_text: str,
) -> None:
    snapshot = zlib.compress(
        json.dumps({"user_content": user_text}, ensure_ascii=False).encode("utf-8")
    )
    with sqlite3.connect(raw_db) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_turn_revisions (
                revision_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                snapshot_blob BLOB NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO raw_turn_revisions (
                revision_id, content_hash, snapshot_blob
            ) VALUES (?, ?, ?)
            """,
            (revision_id, _sha256(user_text), snapshot),
        )


def _catalog(*, revision_id: str, user_text: str, role: str = "user", **metadata):
    from core.evidence.source_authority import SourceAuthorityCatalog

    return SourceAuthorityCatalog.from_messages(
        (
            {
                "role": role,
                "content": user_text,
                "source_span": {
                    "revision_id": revision_id,
                    "span_start": 0,
                    "span_end": len(user_text),
                    "role": role,
                    "content_hash": _sha256(user_text),
                },
                **metadata,
            },
        ),
        allowed_source_event_ids=(revision_id,),
    )


def _principal():
    from core.access_policy import PrincipalEnvelope

    return PrincipalEnvelope(
        principal_id="mcp:codex:persona-producer",
        agent="codex",
        host_kind="codex",
        capability_id="persona-producer",
        capabilities=frozenset({"memory_read"}),
    )


def _narrowing():
    from core.access_policy import AccessNarrowing

    return AccessNarrowing(session_id="persona-producer-session")


def _record(
    *,
    raw_db: Path,
    store,
    catalog,
    quote: str,
    dimension: str = "interaction_contract",
    assertion_id: str = "",
    expected_revision_id: str = "",
):
    from core.application.persona import PersonaApplicationService

    entry = catalog.entries[0]
    return PersonaApplicationService().record_explicit_profile_evidence(
        source_authority_catalog=catalog,
        source_authority_id=entry.source_authority_id,
        raw_db_path=raw_db,
        principal=_principal(),
        narrowing=_narrowing(),
        signal_store=store,
        signal_type="explicit_preference",
        dimension=dimension,
        quote=quote,
        confidence=0.91,
        assertion_id=assertion_id,
        expected_revision_id=expected_revision_id,
    )


def test_profile_producer_replay_is_idempotent_and_uses_exact_signal_ref(tmp_path: Path) -> None:
    from core.persona.psyche import SignalStore

    raw_db = tmp_path / "raw_events.db"
    revision_id = "raw-revision-profile-producer"
    quote = "每个问题都要先修复、测试、深审，再提交本地仓库。"
    _write_raw_revision(raw_db, revision_id=revision_id, user_text=quote)
    catalog = _catalog(revision_id=revision_id, user_text=quote)
    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    try:
        results = [
            _record(raw_db=raw_db, store=store, catalog=catalog, quote=quote)
            for _ in range(10)
        ]
        assert {item["signal_id"] for item in results} == {1}
        assert len({item["assertion_id"] for item in results}) == 1

        conn = store._pool.get_conn()
        assert conn.execute("SELECT COUNT(*) FROM profile_signals").fetchone()[0] == 1
        assertion_row = conn.execute(
            "SELECT supporting_signals FROM profile_assertions"
        ).fetchone()
        assert json.loads(assertion_row[0]) == ["profile_signals:1"]
    finally:
        store.close()


def test_profile_producer_changed_revision_of_same_event_creates_new_signal(tmp_path: Path) -> None:
    from core.persona.psyche import SignalStore

    raw_db = tmp_path / "raw_events.db"
    revision_id = "raw-revision-profile-changed"
    first_quote = "我希望结论必须附带可复现的验证命令。"
    corrected_quote = "我希望结论必须附带可复现的验证命令和失败边界。"
    _write_raw_revision(raw_db, revision_id=revision_id, user_text=first_quote)
    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    try:
        first = _record(
            raw_db=raw_db,
            store=store,
            catalog=_catalog(revision_id=revision_id, user_text=first_quote),
            quote=first_quote,
            dimension="judgment_standard",
        )
        # The logical source event is unchanged while its immutable revision
        # binding changes.  The identity must therefore create a new signal,
        # not a replay duplicate of the first source revision.
        _write_raw_revision(raw_db, revision_id=revision_id, user_text=corrected_quote)
        first_revision_id = store.get_profile_assertion_revisions(first["assertion_id"])[-1][
            "revision_id"
        ]
        second = _record(
            raw_db=raw_db,
            store=store,
            catalog=_catalog(revision_id=revision_id, user_text=corrected_quote),
            quote=corrected_quote,
            dimension="judgment_standard",
            assertion_id=first["assertion_id"],
            expected_revision_id=first_revision_id,
        )
        assert second["signal_id"] == 2
        conn = store._pool.get_conn()
        assert conn.execute("SELECT COUNT(*) FROM profile_signals").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(DISTINCT source_identity) FROM profile_signals"
        ).fetchone()[0] == 2
    finally:
        store.close()


@pytest.mark.parametrize(
    ("role", "metadata"),
    (
        ("assistant", {}),
        ("tool", {}),
        ("user", {"asset_kind": "trusted_user_document"}),
    ),
)
def test_profile_producer_rejects_non_user_authority(
    tmp_path: Path,
    role: str,
    metadata: dict[str, str],
) -> None:
    from core.persona.psyche import SignalStore

    raw_db = tmp_path / "raw_events.db"
    revision_id = f"raw-revision-{role}"
    quote = "不要把这段低权限材料升级为用户画像。"
    _write_raw_revision(raw_db, revision_id=revision_id, user_text=quote)
    catalog = _catalog(
        revision_id=revision_id,
        user_text=quote,
        role=role,
        **metadata,
    )
    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    try:
        with pytest.raises(PermissionError, match="explicit user"):
            _record(raw_db=raw_db, store=store, catalog=catalog, quote=quote)
        assert store._pool.get_conn().execute(
            "SELECT COUNT(*) FROM profile_signals"
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_profile_producer_crash_restart_reuses_same_signal_identity(tmp_path: Path) -> None:
    from core.persona.psyche import SignalStore

    raw_db = tmp_path / "raw_events.db"
    revision_id = "raw-revision-profile-restart"
    quote = "请保留修复路径、测试命令与深审证据。"
    _write_raw_revision(raw_db, revision_id=revision_id, user_text=quote)
    catalog = _catalog(revision_id=revision_id, user_text=quote)
    database = tmp_path / "user_signals.db"

    store = SignalStore(initialize_schema=True, db_path=database)
    try:
        first = _record(raw_db=raw_db, store=store, catalog=catalog, quote=quote)
    finally:
        store.close()

    restarted = SignalStore(db_path=database)
    try:
        second = _record(raw_db=raw_db, store=restarted, catalog=catalog, quote=quote)
        assert second == first
        assert restarted._pool.get_conn().execute(
            "SELECT COUNT(*) FROM profile_signals"
        ).fetchone()[0] == 1
    finally:
        restarted.close()


def test_profile_producer_rejects_quote_outside_the_selected_raw_span(tmp_path: Path) -> None:
    from core.persona.psyche import SignalStore

    raw_db = tmp_path / "raw_events.db"
    revision_id = "raw-revision-profile-quote"
    quote = "每项改动都必须留下可复现的验证证据。"
    _write_raw_revision(raw_db, revision_id=revision_id, user_text=quote)
    catalog = _catalog(revision_id=revision_id, user_text=quote)
    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    try:
        with pytest.raises(ValueError, match="exact selected Raw span"):
            _record(
                raw_db=raw_db,
                store=store,
                catalog=catalog,
                quote="模型生成的、但不在用户原话中的画像结论。",
            )
        assert store._pool.get_conn().execute(
            "SELECT COUNT(*) FROM profile_signals"
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_profile_producer_rolls_back_signal_when_assertion_append_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from core.persona.psyche import SignalStore

    raw_db = tmp_path / "raw_events.db"
    revision_id = "raw-revision-profile-atomic"
    quote = "我要求每一个结论都能回到原始证据验证。"
    _write_raw_revision(raw_db, revision_id=revision_id, user_text=quote)
    catalog = _catalog(revision_id=revision_id, user_text=quote)
    store = SignalStore(initialize_schema=True, db_path=tmp_path / "user_signals.db")
    original = store._cognitive_profiles.upsert_assertion
    try:
        monkeypatch.setattr(
            store._cognitive_profiles,
            "upsert_assertion",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected append failure")),
        )
        with pytest.raises(RuntimeError, match="injected append failure"):
            _record(raw_db=raw_db, store=store, catalog=catalog, quote=quote)
        assert store._pool.get_conn().execute(
            "SELECT COUNT(*) FROM profile_signals"
        ).fetchone()[0] == 0

        monkeypatch.setattr(store._cognitive_profiles, "upsert_assertion", original)
        result = _record(raw_db=raw_db, store=store, catalog=catalog, quote=quote)
        assert result["signal_id"] == 1
        assert store._pool.get_conn().execute(
            "SELECT COUNT(*) FROM profile_signals"
        ).fetchone()[0] == 1
    finally:
        store.close()
