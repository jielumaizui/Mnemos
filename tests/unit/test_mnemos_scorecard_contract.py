from core.system_contracts import SCORECARD_DIMENSIONS, audit_mnemos_scorecard


def test_mnemos_scorecard_is_strictly_valid():
    assert audit_mnemos_scorecard(strict=True) == []


def test_wow_path_dimension_keeps_extended_max_score():
    assert SCORECARD_DIMENSIONS["wow_path"].max_score == 150
    assert SCORECARD_DIMENSIONS["wow_path"].evidence_commands


def test_data_pipeline_scorecard_tracks_runtime_orphans_and_no_source_consumers():
    metrics = SCORECARD_DIMENSIONS["data_pipeline"].runtime_metrics

    assert "producer_consumer.orphan_outputs" in metrics
    assert "producer_consumer.no_source_consumers" in metrics
    assert "producer_consumer.item_mismatches" in metrics
