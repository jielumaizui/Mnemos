from __future__ import annotations

from core.cognitive.auto_calibration import CalibrationReport
from core.cognitive.models import Dimension, Observation, ObservationType
from core.cognitive.observation_projection import rebuild_observation_projection


def test_rebuild_queries_calibration_only_for_bound_observations(tmp_path, monkeypatch):
    bound = Observation(
        dimension=Dimension.ATTENTION,
        observation_type=ObservationType.PATTERN,
        value="bound",
        base_confidence=0.5,
        confidence=0.7,
        calibration_revision_id="calibration-revision",
        calibration_input_hash="calibration-input",
        calibration_spec_hash="calibration-spec",
        calibration_record_hash="calibration-record",
        id="bound-observation",
    )
    unbound = Observation(
        dimension=Dimension.ATTENTION,
        observation_type=ObservationType.PATTERN,
        value="unbound",
        confidence=0.4,
        id="unbound-observation",
    )
    report = CalibrationReport(
        observation_id=bound.id,
        original_confidence=0.5,
        calibrated_confidence=0.7,
        overall_verdict="confirmed",
        validator_spec_hash="calibration-spec",
        calculation_input_hash="calibration-input",
        calibration_revision_id="calibration-revision",
        calibration_record_hash="calibration-record",
    )
    requested_ids = []

    class FakeObservationStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def query_all_for_projection(self, *, dimension):
            return [bound, unbound] if dimension is Dimension.ATTENTION else []

    class FakeCalibrationRecordStore:
        def __init__(self, _state_store):
            pass

        def current_reports(self, observation_ids, *, expected_spec_hash=""):
            requested_ids.extend(observation_ids)
            assert expected_spec_hash == "calibration-spec"
            return {bound.id: report}

    class FakeCalibrationEngine:
        spec_hash = "calibration-spec"

    class FakeWikiExporter:
        def __init__(self, *_args, **_kwargs):
            pass

        def export_batch(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(
        "core.cognitive.observation_projection.ObservationStore",
        FakeObservationStore,
    )
    monkeypatch.setattr(
        "core.cognitive.observation_projection.CognitiveStateStore",
        lambda _path: object(),
    )
    monkeypatch.setattr(
        "core.cognitive.observation_projection.CalibrationRecordStore",
        FakeCalibrationRecordStore,
    )
    monkeypatch.setattr(
        "core.cognitive.observation_projection.CalibrationEngine",
        FakeCalibrationEngine,
    )
    monkeypatch.setattr(
        "core.cognitive.observation_projection.WikiExporter",
        FakeWikiExporter,
    )
    state_path = tmp_path / "cognitive_state.db"
    state_path.touch()

    replay = rebuild_observation_projection(
        wiki_dir=tmp_path / "wiki",
        observation_db_path=tmp_path / "observations.db",
        cognitive_state_db_path=state_path,
    )

    assert requested_ids == [bound.id]
    assert replay.observation_count == 2
