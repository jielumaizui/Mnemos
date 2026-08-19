from datetime import datetime
from unittest.mock import MagicMock

from core.application.observation import ObservationApplicationService
from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.models import Dimension, ObservationType, SourceType


def test_observation_search_authorizes_before_fetching_payload(monkeypatch):
    obs = MagicMock()
    obs.id = "obs-1"
    obs.dimension = Dimension.ATTENTION
    obs.observation_type = ObservationType.FREQUENCY
    obs.value = {"x": 1}
    obs.confidence = 0.8
    obs.source_type = SourceType.WIKI
    obs.source_path = "/wiki/a.md"
    obs.source_id = "s1"
    obs.evidence = ["e1", "e2", "e3", "e4"]
    obs.observed_at = datetime(2026, 1, 1)

    index = MagicMock()
    index.authorized_query.return_value = (
        [obs],
        {"candidate_count": 1, "authorized_count": 1, "denied_by_reason": {}},
    )
    monkeypatch.setattr("core.cognitive.observation_store.ObservationIndex", lambda: index)

    principal = PrincipalEnvelope(
        principal_id="mcp:codex:observation-test",
        agent="codex",
        host_kind="codex",
        capability_id="observation-test",
        capabilities=frozenset({"memory_read"}),
    )

    result = ObservationApplicationService().observation_search(
        dimension="attention",
        source_type="",
        limit=5,
        principal=principal,
        narrowing=AccessNarrowing(session_id="session-1"),
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["observations"][0]["evidence"] == ["e1", "e2", "e3"]
    index.authorized_query.assert_called_once_with(
        principal=principal,
        narrowing=AccessNarrowing(session_id="session-1"),
        purpose="observation_read",
        dimension=Dimension.ATTENTION,
        source_type=None,
        limit=5,
    )
    index.get_by_dimension.assert_not_called()
    index.query.assert_not_called()


def test_observation_search_requires_principal_before_constructing_index(monkeypatch):
    monkeypatch.setattr(
        "core.cognitive.observation_store.ObservationIndex",
        lambda: (_ for _ in ()).throw(AssertionError("index must not be constructed")),
    )

    result = ObservationApplicationService().observation_search(limit=5)

    assert result["success"] is False
    assert result["error_code"] == "principal_required"
