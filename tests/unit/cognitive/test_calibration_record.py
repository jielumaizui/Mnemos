"""COG-049 canonical calibration lineage and persistence tests."""

from __future__ import annotations

from dataclasses import replace
import json
import hashlib
import re
import sqlite3

import pytest

from core.cognitive.auto_calibration import (
    CalibrationEngine,
    ContentSourceValidator,
    ContradictionDetector,
    CrossSourceValidator,
    SampleSizeValidator,
)
from core.cognitive.calibration_record import CalibrationRecordStore, report_from_revision
from core.cognitive.models import (
    Dimension,
    Observation,
    ObservationBatch,
    ObservationType,
    SourceType,
)
from core.cognitive.observation_calibration_schema import (
    inspect_observation_calibration_schema,
    reconcile_observation_calibration_schema,
)
from core.cognitive.observation_store import ObservationStore
from core.cognitive.observation_engine import ObservationEngine
from core.cognitive.sources import ContentSource, SourceItem
from core.cognitive.state_contract import validate_cognitive_state_payload
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.wiki_exporter import WikiExporter
from scripts.reconcile_observation_calibration_state import main as reconcile_main


def _raw(revision_id: str, content: str = "AI evidence") -> SourceItem:
    return SourceItem(
        source_type="raw",
        file_path=f"raw://{revision_id}",
        content=content,
        raw_revision_id=revision_id,
        raw_content_hash="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        source_content_hash="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
    )


def _wiki(
    revision_id: str,
    content: str = "AI summary",
    raw_content: str = "AI evidence",
) -> SourceItem:
    return SourceItem(
        source_type="wiki",
        file_path=f"/wiki/{revision_id}.md",
        content=content,
        frontmatter={
            "raw_event_refs": [
                {
                    "revision_id": revision_id,
                    "span_start": 0,
                    "span_end": len(raw_content),
                    "content_hash": (
                        "sha256:" + hashlib.sha256(raw_content.encode()).hexdigest()
                    ),
                }
            ]
        },
    )


def _multi_root_wiki(*revision_ids: str, content: str = "AI synthesis") -> SourceItem:
    return SourceItem(
        source_type="wiki",
        file_path="/wiki/multi-root.md",
        content=content,
        frontmatter={
            "raw_event_refs": [
                {
                    "revision_id": revision_id,
                    "content_hash": (
                        "sha256:" + hashlib.sha256(b"AI evidence").hexdigest()
                    ),
                    "span_start": 0,
                    "span_end": len(content),
                }
                for revision_id in revision_ids
            ]
        },
    )


def _observation() -> Observation:
    return Observation(
        id="obs-calibration-1",
        dimension=Dimension.ATTENTION,
        observation_type=ObservationType.FREQUENCY,
        value={"concepts": {"ai": 2}, "dominant": "ai", "total_mentions": 2},
        confidence=0.6,
        source_id="aggregate",
        source_path="aggregated:raw:1,wiki:1",
        evidence=["AI evidence"],
        source_span_ids=["raw-span:raw-1:0:11"],
    )


@pytest.fixture
def stores(tmp_path):
    observation_store = ObservationStore(str(tmp_path / "observations.db"))
    state_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_path)
    state_store = CognitiveStateStore(state_path)
    return observation_store, state_store


def test_raw_and_derived_wiki_are_one_independent_cluster():
    report = CalibrationEngine(validators=[CrossSourceValidator()]).calibrate(
        _observation(),
        [_observation()],
        [_raw("raw-1"), _wiki("raw-1")],
    )

    assert report.derived_source_double_count == 0
    assert report.derived_members_deduplicated == 1
    assert len(report.independent_evidence_clusters) == 1
    assert report.validations[0].verdict == "questionable"


def test_two_raw_roots_are_independent_clusters():
    report = CalibrationEngine(validators=[CrossSourceValidator()]).calibrate(
        _observation(),
        [_observation()],
        [_raw("raw-1"), _raw("raw-2")],
    )

    assert len(report.independent_evidence_clusters) == 2
    assert report.validations[0].verdict == "confirmed"
    assert len(report.supporting_evidence) == 2


def test_multi_root_derived_page_does_not_merge_independent_raw_roots():
    report = CalibrationEngine(validators=[CrossSourceValidator()]).calibrate(
        _observation(),
        [_observation()],
        [_raw("raw-1"), _raw("raw-2"), _multi_root_wiki("raw-1", "raw-2")],
    )

    assert len(report.independent_evidence_clusters) == 2
    assert len(report.supporting_evidence) == 2
    assert report.derived_members_deduplicated == 1


def test_missing_or_mismatched_derived_lineage_hash_fails_closed():
    malformed = SourceItem(
        source_type="wiki",
        file_path="/wiki/malformed.md",
        content="AI summary",
        frontmatter={
            "raw_event_refs": [
                {"revision_id": "raw-1", "span_start": 0, "span_end": 11}
            ]
        },
    )
    malformed_report = CalibrationEngine(validators=[CrossSourceValidator()]).calibrate(
        _observation(),
        [_observation()],
        [malformed],
    )
    mismatch_report = CalibrationEngine(validators=[CrossSourceValidator()]).calibrate(
        _observation(),
        [_observation()],
        [_raw("raw-1"), _wiki("raw-1", raw_content="different raw")],
    )

    assert malformed.lineage_status == "malformed"
    assert malformed_report.independent_evidence_clusters == []
    assert mismatch_report.independent_evidence_clusters == []
    assert mismatch_report.validations[0].verdict == "inconclusive"


def test_short_fake_lineage_hash_never_becomes_independent():
    malformed = SourceItem(
        source_type="wiki",
        file_path="/wiki/fake-hash.md",
        content="AI summary",
        frontmatter={
            "raw_event_refs": [
                {
                    "revision_id": "raw-1",
                    "content_hash": "sha256:not-a-real-digest",
                    "span_start": 0,
                    "span_end": 11,
                }
            ]
        },
    )

    report = CalibrationEngine(validators=[CrossSourceValidator()]).calibrate(
        _observation(),
        [_observation()],
        [malformed],
    )

    assert malformed.lineage_status == "malformed"
    assert report.independent_evidence_clusters == []


def test_counter_evidence_and_sample_size_use_independent_lineage_only():
    observation = _observation()
    report = CalibrationEngine(
        validators=[CrossSourceValidator(), SampleSizeValidator()]
    ).calibrate(
        observation,
        [observation],
        [_raw("raw-1"), _wiki("raw-1"), _raw("raw-2", "gardening only")],
    )

    assert len(report.independent_evidence_clusters) == 2
    assert len(report.supporting_evidence) == 1
    assert len(report.counter_evidence) == 1
    sample_result = next(
        result for result in report.validations if result.validator_name == "sample_size"
    )
    assert "2 个独立 lineage cluster" in sample_result.reason


def test_calculation_input_binds_peer_observations_and_source_classification():
    observation = _observation()
    observation.content_source = ContentSource.NATIVE_DIALOGUE
    low_completion = Observation(
        id="peer-actions",
        dimension=Dimension.ACTIONS,
        observation_type=ObservationType.RATIO,
        value={"completion_rate": 0.1},
    )
    high_completion = Observation(
        id="peer-actions",
        dimension=Dimension.ACTIONS,
        observation_type=ObservationType.RATIO,
        value={"completion_rate": 0.9},
    )
    source = _raw("raw-1")
    source.content_source = ContentSource.NATIVE_DIALOGUE
    engine = CalibrationEngine(
        validators=[ContradictionDetector(), ContentSourceValidator()]
    )

    contradicted = engine.calibrate(observation, [observation, low_completion], [source])
    supported = engine.calibrate(observation, [observation, high_completion], [source])
    observation.content_source = ContentSource.LIKELY_PASTED
    reclassified = engine.calibrate(
        observation,
        [observation, high_completion],
        [source],
    )

    assert contradicted.calculation_input_hash != supported.calculation_input_hash
    assert contradicted.calibrated_confidence != supported.calibrated_confidence
    assert supported.calculation_input_hash != reclassified.calculation_input_hash
    assert supported.calibrated_confidence != reclassified.calibrated_confidence
    assert supported.input_snapshot["peer_observations"]
    assert supported.input_snapshot["observation"]["content_source"] == "native_dialogue"


def test_validator_runtime_error_fails_closed():
    class BrokenValidator(CrossSourceValidator):
        name = "broken"

        def validate(self, obs, all_observations, source_items):
            raise RuntimeError("validator defect")

    with pytest.raises(RuntimeError, match="validator defect"):
        CalibrationEngine(validators=[BrokenValidator()]).calibrate(
            _observation(),
            [_observation()],
            [_raw("raw-1")],
        )


def test_calibration_spec_fails_closed_when_source_code_is_unavailable(monkeypatch):
    def unavailable(_value):
        raise OSError("source unavailable")

    monkeypatch.setattr(
        "core.cognitive.auto_calibration.inspect.getsource",
        unavailable,
    )

    with pytest.raises(RuntimeError, match="implementation source is unavailable"):
        CalibrationEngine(validators=[CrossSourceValidator()])


def test_duplicate_validator_names_are_rejected():
    with pytest.raises(ValueError, match="validator names must be unique"):
        CalibrationEngine(
            validators=[CrossSourceValidator(), CrossSourceValidator()]
        )


def test_spec_hash_binds_calibration_implementation_code():
    implementation_hashes = CalibrationEngine().spec_payload["implementation_hashes"]

    assert set(implementation_hashes) == {
        "calibration_engine",
        "calibration_math_module",
        "lineage_module",
    }
    assert all(value.startswith("sha256:") for value in implementation_hashes.values())


def test_peer_generated_ids_do_not_change_calculation_identity():
    engine = CalibrationEngine(validators=[ContradictionDetector()])
    first_peer = Observation(
        id="transient-a",
        dimension=Dimension.ACTIONS,
        observation_type=ObservationType.RATIO,
        value={"completion_rate": 0.1},
        source_id="raw-1",
    )
    second_peer = Observation(
        id="transient-b",
        dimension=Dimension.ACTIONS,
        observation_type=ObservationType.RATIO,
        value={"completion_rate": 0.1},
        source_id="raw-1",
    )

    first = engine.calibrate(_observation(), [first_peer], [_raw("raw-1")])
    second = engine.calibrate(_observation(), [second_peer], [_raw("raw-1")])

    assert first.calculation_input_hash == second.calculation_input_hash


def test_target_transient_ids_converge_before_measurement_hashing(stores):
    observation_store, _ = stores
    engine = CalibrationEngine(validators=[CrossSourceValidator()])
    first_observation = _observation()
    first_observation.id = "transient-target-a"
    observation_store.save(first_observation)
    first = engine.calibrate(
        first_observation,
        [first_observation],
        [_raw("raw-1")],
    )

    second_observation = _observation()
    second_observation.id = "transient-target-b"
    observation_store.save(second_observation)
    second = engine.calibrate(
        second_observation,
        [second_observation],
        [_raw("raw-1")],
    )

    assert second_observation.id == first_observation.id
    assert second.calculation_input_hash == first.calculation_input_hash


def test_peer_input_order_does_not_change_calculation_identity():
    engine = CalibrationEngine(validators=[ContradictionDetector()])
    action_peer = Observation(
        dimension=Dimension.ACTIONS,
        observation_type=ObservationType.RATIO,
        value={"completion_rate": 0.1},
        source_id="raw-actions",
    )
    growth_peer = Observation(
        dimension=Dimension.GROWTH,
        observation_type=ObservationType.FREQUENCY,
        value={"growth_signals": 5},
        source_id="raw-growth",
    )

    first = engine.calibrate(
        _observation(),
        [action_peer, growth_peer],
        [_raw("raw-1")],
    )
    second = engine.calibrate(
        _observation(),
        [growth_peer, action_peer],
        [_raw("raw-1")],
    )

    assert first.calculation_input_hash == second.calculation_input_hash
    assert first.calibrated_confidence == second.calibrated_confidence


def test_calibration_record_is_committed_before_observation_binding(stores):
    observation_store, state_store = stores
    observation = _observation()
    observation_store.save(observation)
    report = CalibrationEngine().calibrate(
        observation,
        [observation],
        [_raw("raw-1"), _wiki("raw-1")],
    )
    records = CalibrationRecordStore(state_store)

    commit, persisted = records.commit(observation, report)
    assert commit.status == "committed"
    assert state_store.revision(commit.revision_id) is not None
    assert {item["consumer_id"] for item in state_store.pending_commands()} == {
        "observation_index",
        "wiki_projection",
    }

    records.apply_to_observation(observation_store, commit)
    rebound = observation_store.get_by_id(observation.id)
    assert rebound is not None
    assert rebound.base_confidence == pytest.approx(0.6)
    assert rebound.confidence == pytest.approx(persisted.calibrated_confidence)
    assert rebound.calibration_revision_id == commit.revision_id
    with sqlite3.connect(observation_store.db_path) as conn:
        first_updated_at = conn.execute(
            "SELECT updated_at FROM observations WHERE id=?",
            (observation.id,),
        ).fetchone()[0]
    replay = records.apply_to_observation(observation_store, commit)
    with sqlite3.connect(observation_store.db_path) as conn:
        second_updated_at = conn.execute(
            "SELECT updated_at FROM observations WHERE id=?",
            (observation.id,),
        ).fetchone()[0]
    assert replay["changed"] is False
    assert second_updated_at == first_updated_at


def test_destructive_observation_cleanup_cannot_orphan_calibration_records(stores):
    observation_store, state_store = stores
    observation = _observation()
    observation_store.save(observation)
    records = CalibrationRecordStore(state_store)
    commit, _ = records.commit(
        observation,
        CalibrationEngine().calibrate(observation, [observation], [_raw("raw-1")]),
    )
    records.apply_to_observation(observation_store, commit)
    with sqlite3.connect(observation_store.db_path) as conn:
        conn.execute(
            "UPDATE observations SET updated_at='2000-01-01T00:00:00' WHERE id=?",
            (observation.id,),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="coordinated CalibrationRecord retirement"):
        observation_store.cleanup_older_than(30)
    with pytest.raises(RuntimeError, match="cannot orphan committed CalibrationRecords"):
        observation_store.clear_all()


def test_calibration_record_rejects_report_from_different_base_measurement(stores):
    _, state_store = stores
    original = _observation()
    report = CalibrationEngine(validators=[CrossSourceValidator()]).calibrate(
        original,
        [original],
        [_raw("raw-1")],
    )
    changed = _observation()
    changed.value["total_mentions"] = 99

    with pytest.raises(ValueError, match="input does not match the Observation"):
        CalibrationRecordStore(state_store).commit(changed, report)


def test_same_input_and_spec_replay_reuses_revision(stores):
    observation_store, state_store = stores
    observation = _observation()
    observation_store.save(observation)
    engine = CalibrationEngine()
    records = CalibrationRecordStore(state_store)
    first_report = engine.calibrate(observation, [observation], [_raw("raw-1")])
    first, _ = records.commit(observation, first_report)
    second_report = engine.calibrate(observation, [observation], [_raw("raw-1")])
    second, _ = records.commit(observation, second_report)

    assert first.revision_id == second.revision_id
    assert second.status == "existing"


def test_same_input_spec_ignores_observation_version_counter(stores):
    _, state_store = stores
    observation = _observation()
    engine = CalibrationEngine(validators=[CrossSourceValidator()])
    records = CalibrationRecordStore(state_store)
    first, _ = records.commit(
        observation,
        engine.calibrate(observation, [observation], [_raw("raw-1")]),
    )
    observation.version = 99
    second, _ = records.commit(
        observation,
        engine.calibrate(observation, [observation], [_raw("raw-1")]),
    )

    assert second.revision_id == first.revision_id
    assert second.status == "existing"


def test_calibration_record_read_fails_closed_on_payload_corruption(stores):
    _, state_store = stores
    observation = _observation()
    records = CalibrationRecordStore(state_store)
    commit, _ = records.commit(
        observation,
        CalibrationEngine().calibrate(observation, [observation], [_raw("raw-1")]),
    )
    revision = state_store.revision(commit.revision_id)
    assert revision is not None
    corrupted_payload = dict(revision.payload)
    corrupted_payload["posterior"] = 0.99

    with pytest.raises(ValueError, match="payload hash mismatch"):
        report_from_revision(replace(revision, payload=corrupted_payload))


def test_calibration_snapshot_redacts_only_sensitive_literals_and_remains_replayable(stores):
    _, state_store = stores
    observation = _observation()
    provider_value = "sk-" + "1234567890abcdefghijkl"
    observation.value.update(
        {
            "email": "person@example.com",
            "api_key": provider_value,
        }
    )
    report = CalibrationEngine(validators=[CrossSourceValidator()]).calibrate(
        observation,
        [observation],
        [_raw("raw-1")],
    )
    commit, _ = CalibrationRecordStore(state_store).commit(observation, report)
    revision = state_store.revision(commit.revision_id)

    assert revision is not None
    serialized = json.dumps(dict(revision.payload), ensure_ascii=False)
    assert "person@example.com" not in serialized
    assert provider_value not in serialized
    assert "[REDACTED:PERSONAL]" in serialized
    assert "[REDACTED:CREDENTIAL]" in serialized
    validate_cognitive_state_payload("calibration_record", revision.payload)


def test_frozen_snapshot_replay_preserves_privacy_evidence_under_new_spec(stores):
    _, state_store = stores
    observation = _observation()
    observation.value["email"] = "person@example.com"
    source = _raw("raw-1")
    old_engine = CalibrationEngine(validators=[CrossSourceValidator()])
    old_report = old_engine.calibrate(observation, [observation], [source])

    class UpgradedCrossSource(CrossSourceValidator):
        spec_version = "migration-test-v2"

    new_engine = CalibrationEngine(validators=[UpgradedCrossSource()])
    replayed = new_engine.recalibrate_frozen_snapshot(
        observation,
        [observation],
        [source],
        frozen_input_snapshot=old_report.input_snapshot,
        expected_input_hash=old_report.calculation_input_hash,
        valid_from=old_report.valid_from,
        valid_until=old_report.valid_until,
        omission_receipts=old_report.omission_receipts,
    )

    assert replayed.validator_spec_hash == new_engine.spec_hash
    assert replayed.validator_spec_hash != old_report.validator_spec_hash
    assert replayed.calculation_input_hash != old_report.calculation_input_hash
    assert (
        replayed.input_snapshot["privacy_redaction"]
        == old_report.input_snapshot["privacy_redaction"]
    )
    receipt, _ = CalibrationRecordStore(state_store).commit(observation, replayed)
    revision = state_store.revision(receipt.revision_id)
    assert revision is not None
    validate_cognitive_state_payload("calibration_record", revision.payload)


@pytest.mark.parametrize("changed_input", ["observation", "peer", "lineage"])
def test_frozen_snapshot_replay_rejects_reconstructed_input_drift(changed_input):
    observation = _observation()
    peer = Observation(
        id="peer-actions",
        dimension=Dimension.ACTIONS,
        observation_type=ObservationType.RATIO,
        value={"completion_rate": 0.1},
        source_id="peer-source",
    )
    source = _raw("raw-1")
    old_engine = CalibrationEngine(validators=[ContradictionDetector()])
    old_report = old_engine.calibrate(observation, [peer], [source])

    if changed_input == "observation":
        observation.value["dominant"] = "different"
    elif changed_input == "peer":
        peer.value["completion_rate"] = 0.9
    else:
        source = _raw("raw-1", "different evidence")

    with pytest.raises(ValueError, match="does not match reconstructed input"):
        CalibrationEngine(validators=[ContradictionDetector()]).recalibrate_frozen_snapshot(
            observation,
            [peer],
            [source],
            frozen_input_snapshot=old_report.input_snapshot,
            expected_input_hash=old_report.calculation_input_hash,
            valid_from=old_report.valid_from,
            valid_until=old_report.valid_until,
            omission_receipts=old_report.omission_receipts,
        )


def test_pre_redaction_measurement_hash_distinguishes_private_inputs(stores):
    observation_store, _ = stores
    first_secret = "sk-" + "1111111111abcdefghijkl"
    second_secret = "sk-" + "2222222222abcdefghijkl"
    first = _observation()
    first.value["api_key"] = first_secret
    observation_store.save(first)
    first_report = CalibrationEngine(validators=[CrossSourceValidator()]).calibrate(
        first,
        [first],
        [_raw("raw-1")],
    )

    second = _observation()
    second.value["api_key"] = second_secret
    observation_store.save(second)
    second_report = CalibrationEngine(validators=[CrossSourceValidator()]).calibrate(
        second,
        [second],
        [_raw("raw-1")],
    )

    assert first_report.calculation_input_hash != second_report.calculation_input_hash
    assert (
        first_report.input_snapshot["observation"]["measurement_hash"]
        != second_report.input_snapshot["observation"]["measurement_hash"]
    )
    with sqlite3.connect(observation_store.db_path) as conn:
        persisted_value = str(
            conn.execute("SELECT value FROM observations").fetchone()[0]
        )
    assert first_secret not in persisted_value
    assert second_secret not in persisted_value
    assert "[REDACTED:CREDENTIAL]" in persisted_value


def test_spec_upgrade_supersedes_and_marks_old_spec_stale(stores):
    observation_store, state_store = stores
    observation = _observation()
    observation_store.save(observation)
    records = CalibrationRecordStore(state_store)
    first_engine = CalibrationEngine(validators=[CrossSourceValidator()])
    first, _ = records.commit(
        observation,
        first_engine.calibrate(observation, [observation], [_raw("raw-1")]),
    )

    class UpgradedCrossSource(CrossSourceValidator):
        spec_version = "3"

    upgraded_engine = CalibrationEngine(validators=[UpgradedCrossSource()])
    second, _ = records.commit(
        observation,
        upgraded_engine.calibrate(observation, [observation], [_raw("raw-1")]),
    )

    assert second.revision_id != first.revision_id
    old_view = records.current_reports(
        [observation.id], expected_spec_hash=first_engine.spec_hash
    )[observation.id]
    assert old_view.calibration_revision_id == second.revision_id
    assert old_view.stale is True


def test_superseded_calibration_receipt_cannot_rebind_old_posterior(stores):
    observation_store, state_store = stores
    observation = _observation()
    observation_store.save(observation)
    records = CalibrationRecordStore(state_store)
    old_engine = CalibrationEngine(validators=[CrossSourceValidator()])
    old_receipt, _ = records.commit(
        observation,
        old_engine.calibrate(observation, [observation], [_raw("raw-1")]),
    )

    class UpgradedCrossSource(CrossSourceValidator):
        spec_version = "3"

    upgraded_engine = CalibrationEngine(validators=[UpgradedCrossSource()])
    current_receipt, current_report = records.commit(
        observation,
        upgraded_engine.calibrate(observation, [observation], [_raw("raw-1")]),
    )

    with pytest.raises(RuntimeError, match="committed current CalibrationRecord"):
        records.apply_to_observation(observation_store, old_receipt)

    records.apply_to_observation(observation_store, current_receipt)
    rebound = observation_store.get_by_id(observation.id)
    assert rebound is not None
    assert rebound.calibration_revision_id == current_receipt.revision_id
    assert rebound.confidence == current_report.calibrated_confidence


def test_spec_upgrade_marks_dimension_changed_for_incremental_projection(stores, tmp_path):
    observation_store, state_store = stores
    observation = _observation()
    observation_store.save(observation)
    engine = ObservationEngine(
        store=observation_store,
        wiki_dir=str(tmp_path / "wiki"),
        cognitive_state_store=state_store,
        calibration_engine=CalibrationEngine(validators=[CrossSourceValidator()]),
    )
    _, first_changed = engine._calibrate_and_commit(
        [observation],
        [observation],
        [_raw("raw-1")],
        persist=True,
    )
    assert first_changed == {"attention"}
    rebound = observation_store.get_by_id(observation.id)
    assert rebound is not None

    class UpgradedCrossSource(CrossSourceValidator):
        spec_version = "3"

    engine.calibration_engine = CalibrationEngine(validators=[UpgradedCrossSource()])
    _, upgraded_changed = engine._calibrate_and_commit(
        [rebound],
        [rebound],
        [_raw("raw-1")],
        persist=True,
    )

    assert upgraded_changed == {"attention"}
    upgraded = observation_store.get_by_id(observation.id)
    assert upgraded is not None
    assert upgraded.calibration_spec_hash == engine.calibration_engine.spec_hash


def test_full_and_incremental_projection_share_calibration_set_hash(stores, tmp_path):
    observation_store, state_store = stores
    observation = _observation()
    observation_store.save(observation)
    records = CalibrationRecordStore(state_store)
    report = CalibrationEngine().calibrate(
        observation,
        [observation],
        [_raw("raw-1"), _wiki("raw-1")],
    )
    commit, persisted = records.commit(observation, report)
    records.apply_to_observation(observation_store, commit)
    observation = observation_store.get_by_id(observation.id)
    assert observation is not None
    batch = ObservationBatch(observations=[observation])
    exporter = WikiExporter(str(tmp_path / "wiki"))

    first = exporter.export_batch(batch, {observation.id: persisted})[observation.id]
    first_text = (tmp_path / "wiki/L3-Observations/attention.md").read_text()
    second = exporter.export_batch(batch, {observation.id: persisted})[observation.id]
    second_text = (tmp_path / "wiki/L3-Observations/attention.md").read_text()

    assert first.calibration_set_hash == second.calibration_set_hash
    assert re.search(r'calibration_set_hash: "([^\"]+)"', first_text).group(1) == re.search(
        r'calibration_set_hash: "([^\"]+)"', second_text
    ).group(1)
    assert observation.id in second_text
    assert persisted.calibration_revision_id in second_text
    assert "raw-span:raw-1:0:11" in second_text
    assert "omission:" in second_text


def test_projection_limit_cannot_evict_an_older_calibrated_observation(
    stores,
    tmp_path,
    monkeypatch,
):
    observation_store, state_store = stores
    calibrated = _observation()
    calibrated.id = "older-calibrated"
    calibrated.source_id = "older-source"
    observation_store.save(calibrated)
    records = CalibrationRecordStore(state_store)
    receipt, _ = records.commit(
        calibrated,
        CalibrationEngine().calibrate(calibrated, [calibrated], [_raw("raw-1")]),
    )
    records.apply_to_observation(observation_store, receipt)

    newer = _observation()
    newer.id = "newer-uncalibrated"
    newer.source_id = "newer-source"
    observation_store.save(newer)
    monkeypatch.setattr("core.cognitive.observation_index.ALL_OBS_LIMIT", 1)
    assert [value.id for value in observation_store.query(limit=1)] == [newer.id]

    engine = ObservationEngine(
        store=observation_store,
        wiki_dir=str(tmp_path / "wiki"),
        cognitive_state_store=state_store,
    )
    engine._reexport_all(dimensions={"attention"})

    text = (tmp_path / "wiki/L3-Observations/attention.md").read_text()
    assert calibrated.id in text
    assert receipt.revision_id in text
    assert state_store.pending_commands("wiki_projection") == []


def test_wiki_projection_rejects_a_mismatched_observation_pointer(stores, tmp_path):
    observation_store, state_store = stores
    observation = _observation()
    observation_store.save(observation)
    records = CalibrationRecordStore(state_store)
    commit, persisted = records.commit(
        observation,
        CalibrationEngine().calibrate(observation, [observation], [_raw("raw-1")]),
    )
    records.apply_to_observation(observation_store, commit)
    rebound = observation_store.get_by_id(observation.id)
    assert rebound is not None
    rebound.calibration_input_hash = "sha256:" + "f" * 64

    with pytest.raises(ValueError, match="record binding mismatch"):
        WikiExporter(str(tmp_path / "wiki")).export_batch(
            ObservationBatch(observations=[rebound]),
            {rebound.id: persisted},
        )


def test_wiki_projection_applies_narrow_sensitive_redaction(stores, tmp_path):
    observation_store, state_store = stores
    observation = _observation()
    private_value = "do-not-" + "project-this"
    provider_value = "sk-" + "1234567890abcdefghijkl"
    observation.value["email"] = "person@example.com"
    observation.evidence = ["pass" + "word=" + private_value]
    observation.user_notes = "api_" + "key=" + provider_value
    observation_store.save(observation)
    records = CalibrationRecordStore(state_store)
    commit, persisted = records.commit(
        observation,
        CalibrationEngine().calibrate(observation, [observation], [_raw("raw-1")]),
    )
    records.apply_to_observation(observation_store, commit)
    rebound = observation_store.get_by_id(observation.id)
    assert rebound is not None
    WikiExporter(str(tmp_path / "wiki")).export_batch(
        ObservationBatch(observations=[rebound]),
        {rebound.id: persisted},
    )
    text = (tmp_path / "wiki/L3-Observations/attention.md").read_text()

    assert "person@example.com" not in text
    assert private_value not in text
    assert provider_value not in text
    assert "[REDACTED:PERSONAL]" in text
    assert "[REDACTED:CREDENTIAL]" in text


def test_legacy_observation_schema_requires_explicit_reconciliation(tmp_path):
    db_path = tmp_path / "observations.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE observations(id TEXT PRIMARY KEY, confidence REAL)")
        conn.execute("INSERT INTO observations VALUES ('obs-1', 0.7)")
        conn.commit()
        preview = reconcile_observation_calibration_schema(conn, apply=False)
        assert preview["before"]["classification"] == "migration_required"
        applied = reconcile_observation_calibration_schema(conn, apply=True)
        assert applied["after"]["ok"] is True
        row = conn.execute(
            """SELECT confidence, base_confidence, calibration_revision_id,
                      base_measurement_status
               FROM observations"""
        ).fetchone()
        assert row == (0.7, 0.7, "", "historical_unverified")
        assert inspect_observation_calibration_schema(conn)["ok"] is True


def test_historical_unverified_base_cannot_receive_a_posterior(stores):
    store, state_store = stores
    db_path = store.db_path
    observation = _observation()
    store.save(observation)
    records = CalibrationRecordStore(state_store)
    commit, _ = records.commit(
        observation,
        CalibrationEngine().calibrate(observation, [observation], [_raw("raw-1")]),
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE observations SET base_measurement_status='historical_unverified'
            WHERE id=?
            """,
            (observation.id,),
        )
        conn.commit()

    with pytest.raises(ValueError, match="verified base measurement"):
        records.apply_to_observation(store, commit)


def test_new_observation_base_measurement_is_verified(stores):
    observation_store, _ = stores
    observation_store.save(_observation())

    rebound = observation_store.get_by_id("obs-calibration-1")
    assert rebound is not None
    assert rebound.base_measurement_status == "verified"


def test_observation_save_rejects_a_caller_supplied_calibration_pointer(stores):
    observation_store, _ = stores
    observation = _observation()
    observation.confidence = 0.8
    observation.calibration_revision_id = "cogrev-forged"
    observation.calibration_input_hash = "sha256:input"
    observation.calibration_spec_hash = "sha256:spec"
    observation.calibration_record_hash = "sha256:record"

    with pytest.raises(ValueError, match="accepts base measurements only"):
        observation_store.save(observation)


def test_observation_save_rejects_a_mutated_partial_calibration_pointer(stores):
    observation_store, _ = stores
    observation = _observation()
    observation.calibration_input_hash = "sha256:" + "a" * 64

    with pytest.raises(ValueError, match="accepts base measurements only"):
        observation_store.save(observation)


def test_existing_calibration_binding_fails_closed_without_state_store(stores, tmp_path):
    observation_store, state_store = stores
    observation = _observation()
    observation_store.save(observation)
    records = CalibrationRecordStore(state_store)
    commit, _ = records.commit(
        observation,
        CalibrationEngine().calibrate(observation, [observation], [_raw("raw-1")]),
    )
    records.apply_to_observation(observation_store, commit)
    rebound = observation_store.get_by_id(observation.id)
    assert rebound is not None

    engine = ObservationEngine(
        store=observation_store,
        wiki_dir=str(tmp_path / "wiki"),
        cognitive_state_store=state_store,
    )
    engine.calibration_records = None

    with pytest.raises(RuntimeError, match="cannot replay existing calibration bindings"):
        engine._calibrate_and_commit(
            [rebound],
            [rebound],
            [_raw("raw-1")],
            persist=True,
        )


def test_missing_exact_calibration_source_fails_closed():
    observation = _observation()
    observation.source_type = SourceType.RAW
    observation.source_id = "missing-raw"

    with pytest.raises(ValueError, match="source is absent"):
        ObservationEngine._calibration_source_items(
            observation,
            [_raw("different-raw")],
        )


def test_partial_source_span_match_fails_closed():
    observation = _observation()
    observation.source_span_ids = [
        "raw-span:raw-1:0:11",
        "raw-span:missing:0:11",
    ]

    with pytest.raises(ValueError, match="missing exact source spans"):
        ObservationEngine._calibration_source_items(
            observation,
            [_raw("raw-1")],
        )


def test_redacted_source_path_still_selects_the_exact_calibration_source(stores):
    observation_store, _ = stores
    private_source = SourceItem(
        source_type="wiki",
        file_path="/wiki/email=person@example.com/note.md",
        content="AI evidence",
    )
    unrelated = SourceItem(
        source_type="wiki",
        file_path="/wiki/unrelated.md",
        content="other evidence",
    )
    observation = _observation()
    observation.source_type = SourceType.WIKI
    observation.source_path = private_source.file_path
    observation.source_id = "private-wiki"
    observation.source_span_ids = []
    observation_store.save(observation)

    matched = ObservationEngine._calibration_source_items(
        observation,
        [private_source, unrelated],
    )

    assert observation.source_path != private_source.file_path
    assert matched == [private_source]


def test_calibration_schema_rejects_wrong_column_signature(tmp_path):
    db_path = tmp_path / "wrong-signature.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE observations(
                id TEXT PRIMARY KEY,
                confidence REAL,
                base_confidence TEXT NOT NULL DEFAULT '1.0',
                calibration_revision_id TEXT NOT NULL DEFAULT '',
                calibration_input_hash TEXT NOT NULL DEFAULT '',
                calibration_spec_hash TEXT NOT NULL DEFAULT '',
                calibration_record_hash TEXT NOT NULL DEFAULT '',
                source_span_ids TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        report = inspect_observation_calibration_schema(conn)
        assert "base_confidence" in report["column_mismatches"]
        with pytest.raises(ValueError, match="non-canonical signatures"):
            reconcile_observation_calibration_schema(conn, apply=True)


def test_reconciliation_repairs_index_and_partial_pointer(tmp_path):
    db_path = tmp_path / "partial.db"
    store = ObservationStore(str(db_path))
    observation = _observation()
    store.save(observation)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_obs_calibration_revision")
        conn.execute(
            """
            UPDATE observations SET confidence=0.9,
                calibration_revision_id='cogrev-unproven'
            WHERE id=?
            """,
            (observation.id,),
        )
        conn.commit()
        before = inspect_observation_calibration_schema(conn)
        assert before["index_ok"] is False
        assert before["partial_pointer_count"] == 1
        applied = reconcile_observation_calibration_schema(conn, apply=True)
        assert applied["after"]["ok"] is True
        row = conn.execute(
            "SELECT confidence, base_confidence, calibration_revision_id FROM observations"
        ).fetchone()
        assert row == (0.6, 0.6, "")


def test_observation_schema_rejects_non_array_source_span_json(tmp_path):
    db_path = tmp_path / "invalid-spans.db"
    store = ObservationStore(str(db_path))
    observation = _observation()
    store.save(observation)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE observations SET source_span_ids='{}' WHERE id=?",
            (observation.id,),
        )
        conn.commit()
        report = inspect_observation_calibration_schema(conn)

    assert report["ok"] is False
    assert report["invalid_source_span_json_count"] == 1


def test_reconcile_cli_requires_backup_and_preserves_integrity(
    tmp_path,
    monkeypatch,
    capsys,
):
    db_path = tmp_path / "observations.db"
    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(
        "scripts.reconcile_observation_calibration_state.runtime_writers_are_inactive",
        lambda _database_dir: True,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE observations(id TEXT PRIMARY KEY, confidence REAL)")
        conn.execute("INSERT INTO observations VALUES ('obs-1', 0.7)")
        conn.commit()

    exit_code = reconcile_main(
        [
            "--db-path",
            str(db_path),
            "--apply",
            "--backup-dir",
            str(backup_dir),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["backup"]["integrity_check"] == "ok"
    assert list(backup_dir.glob("*.db"))
