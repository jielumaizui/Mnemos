from __future__ import annotations

from dataclasses import asdict
import sqlite3
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.cognitive_decision_fixtures import material_action_authorization


class _Config(SimpleNamespace):
    def get(self, _key, default=None):
        return default


def _config(tmp_path: Path) -> _Config:
    return _Config(database_dir=tmp_path, data_dir=tmp_path, mnemos_dir=tmp_path)


def _insert_persona_revision(
    database: Path,
    *,
    revision_id: str,
    version: int,
    make_head: bool = True,
) -> None:
    from core.persona.psyche import SignalStore

    store = SignalStore(initialize_schema=True, db_path=database)
    store.close()
    content_hash = "sha256:" + f"{version:x}".rjust(64, "0")
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            INSERT INTO persona_revisions (
                revision_id, version, content_hash, supersedes_revision_id,
                source_cursor, materiality_evidence, generated_at,
                period_start, period_end, energy_profile, cognitive_profile,
                value_profile, blindspot_profile, signal_count_used,
                user_confirmed
            ) VALUES (?, ?, ?, NULL, '{}', '{}', ?, ?, ?, '{}', '{}', '{}', '{}', 0, 0)
            """,
            (
                revision_id,
                version,
                content_hash,
                f"2026-07-{20 + version:02d}T00:00:00+00:00",
                "2026-07-01",
                "2026-07-31",
            ),
        )
        if make_head:
            conn.execute(
                """
                INSERT INTO persona_revision_heads(scope_key, revision_id, updated_at)
                VALUES ('global', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(scope_key) DO UPDATE SET
                    revision_id=excluded.revision_id,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (revision_id,),
            )


def _seal_decision(tmp_path: Path) -> None:
    material_action_authorization(
        tmp_path,
        action_type="test.persona_challenge.source",
        owner="tests.persona_challenge",
        executor="tests.persona_challenge",
        target_ref="test://persona-challenge/source",
        input_hash="sha256:" + "1" * 64,
        nonce="persona-challenge-queue",
    )


def _audit(database: Path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "audit_persona_challenge_queue.py"
    spec = importlib.util.spec_from_file_location("audit_persona_challenge_queue", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.audit_persona_challenge_queue(database)


class _Manager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def analyze_and_update(self, session_context, user_options, persona):
        self.calls.append(
            {
                "session_context": session_context,
                "user_options": user_options,
                "persona_version": persona.version,
            }
        )
        return []


class _PositiveManager(_Manager):
    def __init__(self, blindspot) -> None:
        super().__init__()
        self.blindspot = blindspot

    def analyze_and_update(self, session_context, user_options, persona):
        from core.persona.hamartia import BlindSpotProfileManager

        super().analyze_and_update(session_context, user_options, persona)
        challenge = BlindSpotProfileManager._canonical_challenge_for_context(
            self.blindspot,
            session_context,
        )
        assert challenge is not None
        return [challenge]


def _insert_canonical_blindspot(tmp_path: Path):
    from core.cognitive.user_model_asset_store import (
        USER_COGNITIVE_BLINDSPOT_SPEC,
        UserCognitiveBlindspotStore,
        initialize_asset_store,
    )
    from core.cognitive.user_model_assets import AssetScope, UserCognitiveBlindspot

    path = tmp_path / "user_cognitive_blindspots.db"
    initialize_asset_store(path, USER_COGNITIVE_BLINDSPOT_SPEC)
    blindspot = UserCognitiveBlindspot.create(
        blindspot_type="framing",
        description="两个候选方案共享了同一个未经验证的前提。",
        evidence_refs=("decision:shared-premise",),
        user_goal_ref="goal:choose-safe-option",
        impact="可能排除不依赖该前提的更安全方案。",
        scope=AssetScope(
            scope_type="project",
            scope_id="mnemos",
            purpose="decision_support",
            principal_id="mcp:codex:material-sink-test",
        ),
        confidence=0.88,
        expires_at="2099-01-01T00:00:00+00:00",
        invalidation_condition="后续候选集合包含相互独立的前提。",
        authority_evidence_refs=("source-authority:explicit-user",),
        admission_command_id="blindspot-admission-command",
        admission_command_hash="sha256:" + "a" * 64,
        admission_idempotency_key="blindspot-positive-command",
        decision_context={
            "decision_id": "material-decision",
            "decision_trace_revision_id": "material-decision-revision",
            "decision_trace_hash": "sha256:" + "b" * 64,
            "session_id": "material-session",
            "project_id": "mnemos",
            "persona_revision_id": "persona-revision:1:challenge",
        },
    )
    store = UserCognitiveBlindspotStore(path)
    assert store.append_initial(
        asdict(blindspot),
        evidence_refs=(
            *blindspot.evidence_refs,
            *blindspot.authority_evidence_refs,
        ),
        authority_evidence=(
            {
                "source_authority_id": "source-authority:explicit-user",
                "authority": "explicit_user",
            },
        ),
        scope_type=blindspot.scope_type,
        scope_id=blindspot.scope_id,
        purpose=blindspot.purpose,
        principal_id=blindspot.principal_id,
        expires_at=blindspot.expires_at,
        invalidation_condition=blindspot.invalidation_condition,
        consumers=blindspot.consumers,
    )
    return blindspot


def test_twenty_four_empty_ticks_make_no_business_writes(tmp_path: Path) -> None:
    from core.persona.challenge_queue import PersonaChallengeQueueConsumer

    consumer = PersonaChallengeQueueConsumer(_config(tmp_path))

    results = [consumer.run_once() for _ in range(24)]

    assert all(result["status"] == "noop" for result in results)
    assert all(result["reason"] == "no_pending_decision_command" for result in results)
    assert list(tmp_path.iterdir()) == []


def test_eligible_decision_is_atomically_queued_and_consumed_once(tmp_path: Path) -> None:
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from core.cognitive.decision_trace import DecisionTraceStore
    from core.cognitive.state_store import CognitiveStateStore
    from core.persona.challenge_queue import (
        PERSONA_CHALLENGE_COMMAND,
        PERSONA_CHALLENGE_CONSUMER,
        PersonaChallengeQueueConsumer,
    )

    _insert_persona_revision(
        tmp_path / "user_signals.db",
        revision_id="persona-revision:1:challenge",
        version=1,
    )
    _seal_decision(tmp_path)
    state = CognitiveStateStore(_config(tmp_path))
    commands = state.pending_commands(PERSONA_CHALLENGE_CONSUMER)
    assert len(commands) == 1
    command = commands[0]
    assert command["command_type"] == PERSONA_CHALLENGE_COMMAND
    assert command["payload"]["persona_revision"]["revision_id"] == ("persona-revision:1:challenge")
    assert len(command["payload"]["options"]) == 2
    assert all(
        option["option_hash"].startswith("sha256:") for option in command["payload"]["options"]
    )
    assert command["payload"]["decision_trace"]["revision_id"] == command["revision_id"]
    assert command["payload"]["principal"]["principal_id"]
    assert command["payload"]["scope"]["id"] == "mnemos"
    verified = DecisionTraceStore(state).verify(
        command["revision_id"],
        principal=PrincipalEnvelope(
            principal_id="mcp:codex:material-sink-test",
            agent="codex",
            host_kind="test",
            capability_id="material-sink-test",
            capabilities=frozenset({"memory_read", "memory_write"}),
            allowed_projects=frozenset({"mnemos"}),
        ),
        narrowing=AccessNarrowing(project="mnemos"),
    )
    assert verified.status == "verified"

    manager = _Manager()
    result = PersonaChallengeQueueConsumer(
        _config(tmp_path),
        manager_factory=lambda _store: manager,
    ).run_once()

    assert result["status"] == "consumed"
    assert result["challenges"] == 0
    assert result["reason"] == "no_admitted_canonical_revision"
    assert len(manager.calls) == 1
    assert state.pending_commands(PERSONA_CHALLENGE_CONSUMER) == []
    assert len(state.effect_receipts_for_revision(command["revision_id"])) == 1
    _seal_decision(tmp_path)
    assert state.pending_commands(PERSONA_CHALLENGE_CONSUMER) == []
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        replay_duplicates = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT revision_id, consumer_id, command_type
                FROM cognitive_state_outbox
                WHERE consumer_id=?
                GROUP BY revision_id, consumer_id, command_type
                HAVING COUNT(*) > 1
            )
            """,
            (PERSONA_CHALLENGE_CONSUMER,),
        ).fetchone()[0]
        challenge_without_trace = conn.execute(
            """
            SELECT COUNT(*)
            FROM cognitive_state_outbox AS command
            LEFT JOIN cognitive_state_revisions AS revision
              ON revision.revision_id=command.revision_id
             AND revision.object_type='decision_trace'
            WHERE command.consumer_id=? AND revision.revision_id IS NULL
            """,
            (PERSONA_CHALLENGE_CONSUMER,),
        ).fetchone()[0]
    assert replay_duplicates == 0
    assert challenge_without_trace == 0
    audit = _audit(tmp_path / "producer_consumer_ledger.db")
    assert audit["ok"] is True
    assert audit["empty_tick_business_writes"] == 0
    assert audit["eligible_decision_command_consumed"] == 1
    assert audit["challenge_command_replay_duplicates"] == 0
    assert audit["challenge_without_decision_trace"] == 0


def test_real_canonical_challenge_command_is_presented_and_committed(
    tmp_path: Path,
) -> None:
    from core.cognitive.state_store import CognitiveStateStore
    from core.persona.challenge_queue import (
        PERSONA_CHALLENGE_CONSUMER,
        PersonaChallengeQueueConsumer,
    )

    _insert_persona_revision(
        tmp_path / "user_signals.db",
        revision_id="persona-revision:1:challenge",
        version=1,
    )
    blindspot = _insert_canonical_blindspot(tmp_path)
    _seal_decision(tmp_path)
    manager = _PositiveManager(blindspot)
    consumer = PersonaChallengeQueueConsumer(
        _config(tmp_path),
        manager_factory=lambda _store: manager,
    )

    pending = consumer.run_once()

    assert pending["status"] == "awaiting_presentation"
    assert pending["reason"] == "delivery_pending_presentation"
    assert pending["challenges"] == 1
    assert pending["consumed"] == 0
    assert len(pending["delivery_ids"]) == 1
    receipt = consumer.record_presentation(
        command_id=pending["command_id"],
        delivery_ids=pending["delivery_ids"],
        host_agent="codex",
        rendered_content_hash=pending["rendered_content_hash"],
    )

    assert receipt["schema_version"] == "mnemos.persona_challenge_presentation.v1"
    assert receipt["receipt_hash"].startswith("sha256:")
    state = CognitiveStateStore(_config(tmp_path))
    assert state.pending_commands(PERSONA_CHALLENGE_CONSUMER) == []
    effect = state.effect_receipt(pending["command_id"])
    assert effect is not None
    assert effect["status"] == "committed"
    audit = _audit(tmp_path / "producer_consumer_ledger.db")
    assert audit["eligible_decision_command_consumed"] == 1
    assert audit["challenge_without_decision_trace"] == 0


def test_stale_persona_revision_is_closed_without_running_challenge(tmp_path: Path) -> None:
    from core.cognitive.state_store import CognitiveStateStore
    from core.persona.challenge_queue import (
        PERSONA_CHALLENGE_CONSUMER,
        PersonaChallengeQueueConsumer,
    )

    database = tmp_path / "user_signals.db"
    _insert_persona_revision(
        database,
        revision_id="persona-revision:1:challenge",
        version=1,
    )
    _seal_decision(tmp_path)
    _insert_persona_revision(
        database,
        revision_id="persona-revision:2:challenge",
        version=2,
    )
    manager = _Manager()

    result = PersonaChallengeQueueConsumer(
        _config(tmp_path),
        manager_factory=lambda _store: manager,
    ).run_once()

    assert result["status"] == "intentional_skip"
    assert result["reason"] == "stale_persona_revision"
    assert manager.calls == []
    assert CognitiveStateStore(_config(tmp_path)).pending_commands(PERSONA_CHALLENGE_CONSUMER) == []


def test_crash_after_command_read_leaves_command_replayable(tmp_path: Path) -> None:
    from core.cognitive.state_store import CognitiveStateStore
    from core.persona.challenge_queue import (
        PERSONA_CHALLENGE_CONSUMER,
        PersonaChallengeQueueConsumer,
    )

    _insert_persona_revision(
        tmp_path / "user_signals.db",
        revision_id="persona-revision:1:challenge",
        version=1,
    )
    _seal_decision(tmp_path)
    manager = _Manager()

    def failpoint(stage: str) -> None:
        if stage == "after_command_read":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        PersonaChallengeQueueConsumer(
            _config(tmp_path),
            manager_factory=lambda _store: manager,
            failpoint=failpoint,
        ).run_once()

    state = CognitiveStateStore(_config(tmp_path))
    assert len(state.pending_commands(PERSONA_CHALLENGE_CONSUMER)) == 1
    assert manager.calls == []
    assert (
        PersonaChallengeQueueConsumer(
            _config(tmp_path),
            manager_factory=lambda _store: manager,
        ).run_once()["status"]
        == "consumed"
    )
    assert len(manager.calls) == 1
    assert state.pending_commands(PERSONA_CHALLENGE_CONSUMER) == []


def test_crash_after_challenge_before_receipt_keeps_one_replayable_command(
    tmp_path: Path,
) -> None:
    from core.cognitive.state_store import CognitiveStateStore
    from core.persona.challenge_queue import (
        PERSONA_CHALLENGE_CONSUMER,
        PersonaChallengeQueueConsumer,
    )

    _insert_persona_revision(
        tmp_path / "user_signals.db",
        revision_id="persona-revision:1:challenge",
        version=1,
    )
    _seal_decision(tmp_path)
    manager = _Manager()

    def failpoint(stage: str) -> None:
        if stage == "after_challenge_before_receipt":
            raise RuntimeError("simulated post-challenge crash")

    with pytest.raises(RuntimeError, match="post-challenge crash"):
        PersonaChallengeQueueConsumer(
            _config(tmp_path),
            manager_factory=lambda _store: manager,
            failpoint=failpoint,
        ).run_once()

    state = CognitiveStateStore(_config(tmp_path))
    assert len(manager.calls) == 1
    assert len(state.pending_commands(PERSONA_CHALLENGE_CONSUMER)) == 1
    assert (
        PersonaChallengeQueueConsumer(
            _config(tmp_path),
            manager_factory=lambda _store: manager,
        ).run_once()["status"]
        == "consumed"
    )
    assert len(manager.calls) == 2
    assert state.pending_commands(PERSONA_CHALLENGE_CONSUMER) == []
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_outbox WHERE consumer_id=?",
                (PERSONA_CHALLENGE_CONSUMER,),
            ).fetchone()[0]
            == 1
        )


def test_one_option_cannot_form_a_challenge_command() -> None:
    from core.persona.challenge_queue import build_persona_challenge_command

    with pytest.raises(ValueError, match="at least two"):
        build_persona_challenge_command(
            decision_revision_id="cogrev-decision",
            decision_id="decision-one-option",
            decision_hash="sha256:" + "d" * 64,
            candidates=({"candidate_id": "only", "key": "only"},),
            persona_revision={
                "revision_id": "persona-revision:1:challenge",
                "content_hash": "sha256:" + "1" * 64,
            },
            principal={"principal_id": "mcp:codex:test", "agent": "codex"},
            scope={"type": "project", "id": "mnemos"},
        )
