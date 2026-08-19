"""Unit tests for core.cognitive.observation_store."""

from datetime import datetime

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.models import Dimension, Observation, ObservationType, SourceType
from core.cognitive.observation_store import ObservationIndex, ObservationStore


@pytest.fixture
def sample_observation() -> Observation:
    return Observation(
        id="obs-001",
        dimension=Dimension.ATTENTION,
        observation_type=ObservationType.FREQUENCY,
        value={"concepts": {"ai": 5}},
        unit="mentions",
        confidence=0.8,
        source_type=SourceType.WIKI,
        source_path="/wiki/attention.md",
        source_id="session-1",
        evidence=["evidence 1"],
        observed_at=datetime(2026, 6, 1, 10, 0, 0),
        period_start=datetime(2026, 6, 1, 0, 0, 0),
        period_end=datetime(2026, 6, 2, 0, 0, 0),
        version=1,
    )


@pytest.fixture
def store(tmp_path) -> ObservationStore:
    """Return an ObservationStore backed by a tmp_path database."""
    db_path = tmp_path / "observations.db"
    return ObservationStore(db_path=str(db_path))


class TestObservationStore:
    """Tests for ObservationStore save/save_batch/query/get_stats."""

    def test_save_inserts_new_observation(self, store, sample_observation):
        result = store.save(sample_observation)
        assert result == "inserted"

        rows = store.query()
        assert len(rows) == 1
        assert rows[0].id == "obs-001"
        assert rows[0].value == {"concepts": {"ai": 5}}
        assert rows[0].access_control["visibility"] == "restricted"

    def test_projection_query_is_unbounded_by_regular_query_limit(self, store):
        for index in range(3):
            store.save(
                Observation(
                    id=f"projection-{index}",
                    dimension=Dimension.ATTENTION,
                    observation_type=ObservationType.FREQUENCY,
                    value=index,
                    source_id=f"source-{index}",
                )
            )

        assert len(store.query(dimension=Dimension.ATTENTION, limit=1)) == 1
        assert len(
            store.query_all_for_projection(dimension=Dimension.ATTENTION)
        ) == 3

    def test_read_only_store_does_not_initialize_missing_database(self, tmp_path):
        missing = tmp_path / "missing.db"
        read_only = ObservationStore(
            str(missing),
            initialize=False,
            read_only=True,
        )

        with pytest.raises(FileNotFoundError):
            read_only.query_all_for_projection()
        assert not missing.exists()

    def test_read_only_store_rejects_cursor_mutation(self, tmp_path):
        db_path = tmp_path / "observations.db"
        writable = ObservationStore(str(db_path))
        read_only = ObservationStore(
            str(db_path),
            initialize=False,
            read_only=True,
        )

        with pytest.raises(PermissionError, match="read-only ObservationStore"):
            read_only.set_source_cursor("raw", {"event_id": "event-1"})

        assert writable.get_source_cursors() == {}

    def test_authorized_query_filters_acl_header_before_observation_payload(self, store):
        observation = Observation(
            id="obs-authorized",
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"private": "measurement"},
            source_id="raw-revision-1",
            access_control=make_cognitive_access_envelope(
                owner_principal_id="source-agent:claude",
                owner_agent="claude",
                scope_type="observation",
                scope_id="obs-authorized",
                session_id="session-1",
                purposes=("observation_read", "preflight_inject"),
                consent_provenance_refs=("raw:raw-revision-1",),
                sensitivity="sensitive",
                retention_policy="observation_retention",
                source_acl_lineage=("sha256:source-1",),
                visibility="agent",
            ),
        )
        store.save(observation)
        principal = PrincipalEnvelope(
            principal_id="mcp:claude:test",
            agent="claude",
            host_kind="claude",
            capability_id="test",
            capabilities=frozenset({"memory_read"}),
        )

        rows, access = store.authorized_query(
            principal=principal,
            narrowing=AccessNarrowing(session_id="session-1"),
            purpose="observation_read",
        )

        assert [row.id for row in rows] == ["obs-authorized"]
        assert rows[0].value == {"private": "measurement"}
        assert access["authorized_count"] == 1

    def test_denied_observation_never_deserializes_payload(self, store, monkeypatch):
        observation = Observation(
            id="obs-denied",
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"private": "measurement"},
            source_id="raw-revision-1",
            access_control=make_cognitive_access_envelope(
                owner_principal_id="source-agent:claude",
                owner_agent="claude",
                scope_type="observation",
                scope_id="obs-denied",
                session_id="session-1",
                purposes=("observation_read",),
                consent_provenance_refs=("raw:raw-revision-1",),
                sensitivity="sensitive",
                retention_policy="observation_retention",
                source_acl_lineage=("sha256:source-1",),
                visibility="agent",
            ),
        )
        store.save(observation)
        monkeypatch.setattr(
            store,
            "_row_to_obs",
            lambda _row: (_ for _ in ()).throw(AssertionError("payload was fetched")),
        )

        rows, access = store.authorized_query(
            principal=PrincipalEnvelope(
                principal_id="mcp:codex:test",
                agent="codex",
                host_kind="codex",
                capability_id="test",
                capabilities=frozenset({"memory_read"}),
            ),
            narrowing=AccessNarrowing(session_id="session-1"),
            purpose="observation_read",
        )

        assert rows == []
        assert access["denied_by_reason"] == {"owner_agent_mismatch": 1}

    def test_subject_delete_uses_acl_header_and_durable_receipt(self, store):
        subject = Observation(
            id="obs-subject-delete",
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"private": "delete me"},
            source_id="raw-subject",
            access_control=make_cognitive_access_envelope(
                owner_principal_id="mcp:codex:subject",
                owner_agent="codex",
                scope_type="observation",
                scope_id="obs-subject-delete",
                session_id="subject-session",
                project="mnemos",
                purposes=("observation_read",),
                consent_provenance_refs=("raw:subject",),
                sensitivity="sensitive",
                retention_policy="observation_retention",
                source_acl_lineage=("sha256:subject",),
                visibility="private",
            ),
        )
        retained = Observation(
            id="obs-retained",
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"private": "keep me"},
            source_id="raw-retained",
            access_control=make_cognitive_access_envelope(
                owner_principal_id="mcp:codex:retained",
                owner_agent="codex",
                scope_type="observation",
                scope_id="obs-retained",
                session_id="other-session",
                project="mnemos",
                purposes=("observation_read",),
                consent_provenance_refs=("raw:retained",),
                sensitivity="sensitive",
                retention_policy="observation_retention",
                source_acl_lineage=("sha256:retained",),
                visibility="private",
            ),
        )
        store.save(subject)
        store.save(retained)

        result = store.delete_subject_scope(
            request_id="delete-observation-subject",
            scope_kind="session",
            scope_value="subject-session",
        )
        retry = store.delete_subject_scope(
            request_id="delete-observation-subject",
            scope_kind="session",
            scope_value="subject-session",
        )

        assert result == {
            "status": "applied",
            "target_count": 1,
            "receipt_count": 1,
            "after_count": 0,
            "unresolved_legacy_count": 0,
            "verified": True,
        }
        assert retry["status"] == "existing"
        assert retry["verified"] is True
        assert store.get_by_id("obs-subject-delete") is None
        assert store.get_by_id("obs-retained") is not None

    def test_unresolved_observation_acl_cannot_verify_scoped_delete(self, store):
        store.save(
            Observation(
                id="obs-unattributed",
                dimension=Dimension.ATTENTION,
                observation_type=ObservationType.FREQUENCY,
                value={"private": "unattributed"},
                source_id="legacy-unattributed",
            )
        )

        result = store.delete_subject_scope(
            request_id="delete-unattributed-observation",
            scope_kind="session",
            scope_value="subject-session",
        )

        assert result["verified"] is False
        assert result["unresolved_legacy_count"] == 1

    def test_save_updates_existing_observation(self, store, sample_observation):
        store.save(sample_observation)

        sample_observation.value = {"concepts": {"ai": 10}}
        sample_observation.confidence = 0.9
        result = store.save(sample_observation)
        assert result == "updated"

        rows = store.query()
        assert len(rows) == 1
        assert rows[0].value == {"concepts": {"ai": 10}}
        assert rows[0].confidence == pytest.approx(0.9)
        assert rows[0].version == 2

    def test_save_unchanged_existing_observation(self, store, sample_observation):
        store.save(sample_observation)
        result = store.save(sample_observation)
        assert result == "unchanged"

        rows = store.query()
        assert len(rows) == 1
        assert rows[0].version == 1

    def test_save_batch(self, store):
        obs1 = Observation(
            id="obs-001",
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"x": 1},
            source_id="s1",
        )
        obs2 = Observation(
            id="obs-002",
            dimension=Dimension.TIME,
            observation_type=ObservationType.TREND,
            value={"y": 2},
            source_id="s2",
        )
        stats = store.save_batch([obs1, obs2])
        assert stats["inserted"] == 2
        assert stats["updated"] == 0
        assert stats["unchanged"] == 0
        assert stats["changed_dimensions"] == {"attention", "time"}
        assert stats["persisted_observation_ids"] == {"obs-001", "obs-002"}
        assert len(store.query()) == 2

    def test_query_by_dimension(self, store):
        obs1 = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"x": 1},
            source_id="s1",
        )
        obs2 = Observation(
            dimension=Dimension.TIME,
            observation_type=ObservationType.TREND,
            value={"y": 2},
            source_id="s2",
        )
        store.save_batch([obs1, obs2])

        attention = store.query(dimension=Dimension.ATTENTION)
        assert len(attention) == 1
        assert attention[0].dimension == Dimension.ATTENTION

    def test_index_get_by_dimension_uses_store_query(self, store):
        obs1 = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"x": 1},
            source_id="s1",
        )
        obs2 = Observation(
            dimension=Dimension.TIME,
            observation_type=ObservationType.TREND,
            value={"y": 2},
            source_id="s2",
        )
        store.save_batch([obs1, obs2])

        attention = ObservationIndex(store).get_by_dimension(Dimension.ATTENTION)

        assert len(attention) == 1
        assert attention[0].dimension == Dimension.ATTENTION

    def test_query_by_source_type_and_period(self, store):
        obs_wiki = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"x": 1},
            source_type=SourceType.WIKI,
            source_id="s1",
            period_start=datetime(2026, 6, 1),
            period_end=datetime(2026, 6, 5),
        )
        obs_raw = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.PATTERN,
            value={"y": 2},
            source_type=SourceType.RAW,
            source_id="s2",
            period_start=datetime(2026, 6, 10),
            period_end=datetime(2026, 6, 15),
        )
        store.save_batch([obs_wiki, obs_raw])

        wiki_rows = store.query(source_type=SourceType.WIKI)
        assert len(wiki_rows) == 1
        assert wiki_rows[0].source_type == SourceType.WIKI

        period_rows = store.query(
            period_start=datetime(2026, 6, 4),
            period_end=datetime(2026, 6, 12),
        )
        assert len(period_rows) == 2

    def test_query_limit(self, store):
        observations = [
            Observation(
                dimension=Dimension.ATTENTION,
                observation_type=ObservationType.FREQUENCY,
                value={"i": i},
                source_id=f"s{i}",
            )
            for i in range(10)
        ]
        store.save_batch(observations)
        assert len(store.query(limit=3)) == 3

    def test_get_stats(self, store):
        obs1 = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"x": 1},
            source_type=SourceType.WIKI,
            source_id="s1",
        )
        obs2 = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.PATTERN,
            value={"y": 2},
            source_type=SourceType.RAW,
            source_id="s2",
        )
        store.save_batch([obs1, obs2])

        stats = store.get_stats()
        assert stats["total_observations"] == 2
        assert stats["by_dimension"] == {"attention": 2}
        assert set(stats["by_source"].keys()) == {"wiki", "raw"}
        assert stats["latest_update"] is not None

    def test_clear_all(self, store, sample_observation):
        store.save(sample_observation)
        assert len(store.query()) == 1
        store.clear_all()
        assert len(store.query()) == 0
