"""Read-only replay of committed Observation rows into the Wiki lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from core.cognitive.auto_calibration import CalibrationEngine, CalibrationReport
from core.cognitive.calibration_record import CalibrationRecordStore
from core.cognitive.models import Dimension, Observation, ObservationBatch
from core.cognitive.observation_store import ObservationStore
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.wiki_exporter import WikiCalibrationProjectionReceipt, WikiExporter
from core.config import get_config
from core.wiki_derived_projection import DerivedProjectionLifecycle


@dataclass(frozen=True)
class ObservationProjectionReplay:
    """Counts and typed receipts produced by one read-only replay."""

    observation_count: int
    dimension_count: int
    receipts: Dict[str, WikiCalibrationProjectionReceipt]


def _projection_max_file_bytes() -> int:
    """Configured L3 projection page cap; 0 keeps the legacy unbounded export."""

    value = get_config().get("observation.projection_max_file_bytes")
    return max(0, int(value or 0))


def rebuild_observation_projection(
    *,
    wiki_dir: Path | str,
    observation_db_path: Path | str,
    cognitive_state_db_path: Path | str,
    lifecycle: DerivedProjectionLifecycle | None = None,
) -> ObservationProjectionReplay:
    """Read canonical stores and publish a full L3 projection without writes back."""

    store = ObservationStore(
        str(observation_db_path),
        initialize=False,
        read_only=True,
    )
    observations: list[Observation] = []
    seen_ids: set[str] = set()
    for dimension in sorted(Dimension, key=lambda item: item.value):
        rows = store.query_all_for_projection(dimension=dimension)
        for observation in rows:
            if observation.id in seen_ids:
                continue
            seen_ids.add(observation.id)
            observations.append(observation)

    reports: Dict[str, CalibrationReport] = {}
    state_path = Path(cognitive_state_db_path).expanduser()
    bound = [item.id for item in observations if item.calibration_revision_id]
    if bound:
        if not state_path.is_file():
            raise RuntimeError(
                "Canonical cognition state store unavailable for calibrated Observations: "
                + ", ".join(sorted(bound))
            )
        calibration_engine = CalibrationEngine()
        reports = CalibrationRecordStore(
            CognitiveStateStore(state_path)
        ).current_reports(
            bound,
            expected_spec_hash=calibration_engine.spec_hash,
        )
        _validate_calibration_bindings(observations, reports)

    batch = ObservationBatch(observations=observations)
    for observation in observations:
        batch.dimension_counts[observation.dimension.value] = (
            batch.dimension_counts.get(observation.dimension.value, 0) + 1
        )
    receipts = WikiExporter(
        str(wiki_dir),
        lifecycle=lifecycle,
        max_file_bytes=_projection_max_file_bytes(),
    ).export_batch(
        batch,
        calibration_reports=reports,
        full=True,
    )
    return ObservationProjectionReplay(
        observation_count=len(observations),
        dimension_count=len(batch.dimension_counts),
        receipts=receipts,
    )


def _validate_calibration_bindings(
    observations: list[Observation],
    reports: Dict[str, CalibrationReport],
) -> None:
    for observation in observations:
        report = reports.get(observation.id)
        if report is None:
            if observation.calibration_revision_id:
                raise RuntimeError(
                    "Observation projection has no committed CalibrationRecord: "
                    f"{observation.id}"
                )
            continue
        if (
            observation.calibration_revision_id != report.calibration_revision_id
            or observation.calibration_input_hash != report.calculation_input_hash
            or observation.calibration_spec_hash != report.validator_spec_hash
            or observation.calibration_record_hash != report.calibration_record_hash
            or abs(observation.base_confidence_value() - float(report.original_confidence))
            > 1e-9
            or abs(float(observation.confidence) - float(report.calibrated_confidence))
            > 1e-9
        ):
            raise RuntimeError(
                "Observation projection points to a non-current CalibrationRecord"
            )
