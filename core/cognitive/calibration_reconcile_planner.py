"""Read-only planner for exact CalibrationRecord provenance migration."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.auto_calibration import CalibrationEngine
from core.cognitive.calibration_record import calibration_record_payload
from core.cognitive.calibration_reconcile_contracts import (
    CalibrationCommandClosure,
    CalibrationReconciliationPaths,
    CalibrationReconciliationPlan,
    CalibrationReplay,
    CalibrationRetirement,
    finalize_plan_hashes,
)
from core.cognitive.models import Dimension, Observation, ObservationType, SourceType
from core.cognitive.sources import ContentSource, SourceItem, UserIntent
from core.cognitive.state_contract import sha256_json, validate_cognitive_state_payload
from core.cognitive.state_store import CognitiveStateStore
from core.privacy.content_redaction import redact_persistence_value
from core.sync_framework.raw_event_reader import decode_raw_revision_snapshot


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _parse_optional_datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value else None


def _measurement_observation(snapshot: Mapping[str, Any]) -> Observation:
    observation = Observation(
        id=str(snapshot["observation_id"]),
        dimension=Dimension(str(snapshot["dimension"])),
        observation_type=ObservationType(str(snapshot["observation_type"])),
        value=snapshot["value"],
        unit=str(snapshot.get("unit") or ""),
        confidence=float(snapshot["base_confidence"]),
        base_confidence=float(snapshot["base_confidence"]),
        base_measurement_status=str(snapshot["base_measurement_status"]),
        source_type=SourceType(str(snapshot["source_type"])),
        source_path=str(snapshot.get("source_path") or ""),
        source_id=str(snapshot.get("source_id") or ""),
        evidence=[str(value) for value in snapshot.get("evidence", ())],
        source_span_ids=[str(value) for value in snapshot.get("source_span_ids", ())],
        period_start=_parse_optional_datetime(snapshot.get("period_start")),
        period_end=_parse_optional_datetime(snapshot.get("period_end")),
        content_source=ContentSource(str(snapshot.get("content_source") or "unknown")),
        user_intent_signal=UserIntent(
            str(snapshot.get("user_intent_signal") or "unknown")
        ),
    )
    observation.calibration_measurement_hash = str(snapshot["measurement_hash"])
    return observation


def _peer_observation(snapshot: Mapping[str, Any]) -> Observation:
    observation = Observation(
        id="peer-" + str(snapshot["measurement_hash"]).split(":", 1)[-1][:16],
        dimension=Dimension(str(snapshot["dimension"])),
        observation_type=ObservationType(str(snapshot["observation_type"])),
        value=snapshot["value"],
        source_type=SourceType(str(snapshot["source_type"])),
        source_id=str(snapshot.get("source_id") or ""),
        content_source=ContentSource(str(snapshot.get("content_source") or "unknown")),
    )
    observation.calibration_peer_hash = str(snapshot["measurement_hash"])
    return observation


def _observation_from_row(row: sqlite3.Row) -> Observation:
    payload = dict(row)
    payload["value"] = json.loads(str(row["value"]))
    if "access_control" in row.keys():
        payload["access_control"] = json.loads(str(row["access_control"] or "{}"))
    return Observation.from_dict(payload)


def _row_hash(row: sqlite3.Row) -> str:
    # ``ObservationStore`` may add the current ACL column after the reviewed
    # plan and before object mutation.  ACL schema backfill is orthogonal to
    # the frozen measurement, so bind every pre-existing semantic column and
    # deliberately exclude only that additive schema field.
    return sha256_json(
        {
            str(key): row[key]
            for key in row.keys()
            if str(key) != "access_control"
        }
    )


def _visible_measurement_snapshot(
    observation: Observation,
    measurement_hash: str,
) -> Mapping[str, Any]:
    payload = observation.calibration_measurement_payload()
    payload["measurement_hash"] = measurement_hash
    redacted = redact_persistence_value(payload)
    if not isinstance(redacted.value, Mapping):
        raise ValueError("Observation measurement did not remain an object")
    return redacted.value


def _historical_access_control(
    observation_id: str,
    evidence_refs: Sequence[str],
) -> dict[str, Any]:
    return make_cognitive_access_envelope(
        owner_principal_id=f"system:observation:{observation_id}",
        owner_agent="system",
        scope_type="observation",
        scope_id=observation_id,
        purposes=("calibration_internal",),
        consent_provenance_refs=(),
        sensitivity="restricted",
        retention_policy="cognitive_state",
        source_acl_lineage=(sha256_json(list(evidence_refs)),),
        visibility="restricted",
        scope_resolution="restricted_unknown",
        consent_status="restricted_unknown",
    )


def _validate_head_payload(revision: Any) -> dict[str, Any]:
    payload = dict(revision.payload)
    if sha256_json(payload) != revision.payload_hash:
        raise ValueError("immutable calibration payload hash mismatch")
    if (
        str(payload.get("observation_id") or "") != revision.object_id
        or str(payload.get("calculation_input_hash") or "")
        != revision.source_content_hash
    ):
        raise ValueError("calibration head identity binding mismatch")
    validation_payload = dict(payload)
    validation_payload.setdefault(
        "access_control",
        _historical_access_control(revision.object_id, revision.evidence_refs),
    )
    validate_cognitive_state_payload("calibration_record", validation_payload)
    return payload


def _load_raw_source(
    raw_connection: sqlite3.Connection,
    frozen_observation: Mapping[str, Any],
    frozen_lineage: Mapping[str, Any],
) -> tuple[SourceItem, str, str]:
    sources = frozen_lineage.get("sources")
    clusters = frozen_lineage.get("clusters")
    if (
        not isinstance(sources, list)
        or len(sources) != 1
        or not isinstance(clusters, list)
        or len(clusters) != 1
    ):
        raise ValueError("replay requires one exact historical Raw lineage source")
    source = sources[0]
    cluster = clusters[0]
    if not isinstance(source, Mapping) or not isinstance(cluster, Mapping):
        raise ValueError("frozen Raw lineage is malformed")
    source_ref = str(source.get("source_ref") or "")
    if source.get("source_type") != "raw" or not source_ref.startswith("raw-revision:"):
        raise ValueError("frozen lineage source is not canonical Raw")
    revision_id = source_ref.removeprefix("raw-revision:")
    row = raw_connection.execute(
        """
        SELECT r.content_hash, r.snapshot_blob
        FROM raw_turn_revisions AS r
        WHERE r.revision_id=?
        """,
        (revision_id,),
    ).fetchone()
    if row is None:
        raise ValueError("exact Raw revision is unavailable")
    snapshot = decode_raw_revision_snapshot(row["snapshot_blob"])
    raw_content_hash = str(row["content_hash"] or "")
    if (
        raw_content_hash != str(source.get("content_hash") or "")
        or str(snapshot.get("content_hash") or "") != raw_content_hash
    ):
        raise ValueError("Raw revision content hash does not match frozen lineage")
    user_content = str(snapshot.get("user_content") or "")
    text_hash = "sha256:" + hashlib.sha256(user_content.encode("utf-8")).hexdigest()
    if text_hash != str(cluster.get("canonical_text_hash") or ""):
        raise ValueError("Raw visible content does not match frozen lineage text hash")
    item = SourceItem(
        source_type="raw",
        file_path=str(frozen_observation.get("source_path") or f"raw://{revision_id}"),
        content=user_content,
        content_source=ContentSource(
            str(source.get("content_source") or "unknown")
        ),
        user_intent=UserIntent(str(source.get("user_intent") or "unknown")),
        session_id=str(snapshot.get("session_id") or ""),
        raw_event_id=str(snapshot.get("event_id") or ""),
        raw_revision_id=revision_id,
        raw_content_hash=raw_content_hash,
        source_content_hash=str(source.get("content_hash") or ""),
    )
    return item, revision_id, raw_content_hash


def _is_exact_system_collision(
    payload: Mapping[str, Any],
    observation: Observation,
) -> bool:
    frozen = payload.get("input_snapshot", {}).get("observation", {})
    if not isinstance(frozen, Mapping):
        return False
    old_value = frozen.get("value")
    current_value = observation.value
    contract = {
        "object_id_equal": str(frozen.get("observation_id") or "") == observation.id,
        "old_source_id": frozen.get("source_id") == "system",
        "old_source_path": frozen.get("source_path") == "system:user_intent_stats",
        "current_source_id": observation.source_id == "system",
        "current_source_path": observation.source_path == "system:content_source_stats",
        "current_pointer_empty": not observation.calibration_revision_id,
        "old_signal_shape": isinstance(old_value, Mapping)
        and "user_intent_distribution" in old_value,
        "current_signal_shape": isinstance(current_value, Mapping)
        and "user_intent_distribution" in current_value,
        "base_confidence_equal": abs(
            observation.base_confidence_value() - float(payload.get("prior", -1.0))
        )
        < 1e-9,
    }
    return all(contract.values())


def _collision_contract_hash(payload: Mapping[str, Any], observation: Observation) -> str:
    frozen = payload["input_snapshot"]["observation"]
    return sha256_json(
        {
            "object_id": observation.id,
            "old_source_id": frozen["source_id"],
            "old_source_path": frozen["source_path"],
            "old_measurement_hash": frozen["measurement_hash"],
            "current_source_id": observation.source_id,
            "current_source_path": observation.source_path,
            "current_measurement": observation.calibration_measurement_payload(),
        }
    )


def _blocked(object_id: str, revision_id: str, exc: BaseException) -> dict[str, str]:
    return {
        "object_id": object_id,
        "revision_id": revision_id,
        "reason": str(exc),
    }


def build_calibration_reconciliation_plan(
    paths: CalibrationReconciliationPaths,
    *,
    engine: CalibrationEngine | None = None,
) -> CalibrationReconciliationPlan:
    """Reconstruct and validate every current CalibrationRecord without writes."""

    for path in (paths.state_path, paths.observations_path, paths.raw_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    current_engine = engine or CalibrationEngine()
    state_store = CognitiveStateStore(paths.state_path)
    revisions = state_store.current_revisions(object_type="calibration_record")
    with _connect_read_only(paths.observations_path) as observation_connection:
        observation_rows = {
            str(row["id"]): row
            for row in observation_connection.execute(
                "SELECT * FROM observations ORDER BY id"
            ).fetchall()
        }
    peer_cache: dict[str, tuple[list[Observation], list[Any]]] = {}
    replays: list[CalibrationReplay] = []
    retirements: list[CalibrationRetirement] = []
    current_manifests: list[Mapping[str, Any]] = []
    blocked: list[Mapping[str, str]] = []
    with _connect_read_only(paths.raw_path) as raw_connection:
        for revision in revisions:
            try:
                payload = _validate_head_payload(revision)
                row = observation_rows.get(revision.object_id)
                if row is None:
                    raise ValueError("current CalibrationRecord has no Observation row")
                persisted_observation = _observation_from_row(row)
                row_hash = _row_hash(row)
                frozen_input = payload.get("input_snapshot")
                if not isinstance(frozen_input, Mapping):
                    raise ValueError("calibration input snapshot is missing")
                frozen_observation = frozen_input.get("observation")
                if not isinstance(frozen_observation, Mapping):
                    raise ValueError("calibration Observation snapshot is missing")

                if _is_exact_system_collision(payload, persisted_observation):
                    retirement = CalibrationRetirement(
                        object_id=revision.object_id,
                        old_revision_id=revision.revision_id,
                        old_payload_hash=revision.payload_hash,
                        observation_row_hash=row_hash,
                        collision_contract_hash=_collision_contract_hash(
                            payload,
                            persisted_observation,
                        ),
                    )
                    retirements.append(retirement)
                    continue

                if (
                    persisted_observation.calibration_revision_id != revision.revision_id
                    or persisted_observation.calibration_record_hash != revision.payload_hash
                ):
                    raise ValueError("Observation pointer does not bind the current head")
                measurement_hash = str(frozen_observation.get("measurement_hash") or "")
                if (
                    _visible_measurement_snapshot(
                        persisted_observation,
                        measurement_hash,
                    )
                    != frozen_observation
                ):
                    raise ValueError("Observation row drifted from frozen calibration input")
                if (
                    str(payload.get("validator_spec_hash") or "")
                    == current_engine.spec_hash
                    and "access_control" in payload
                ):
                    current_manifests.append(
                        {
                            "action": "already_current",
                            "object_id": revision.object_id,
                            "revision_id": revision.revision_id,
                            "payload_hash": revision.payload_hash,
                            "observation_row_hash": row_hash,
                        }
                    )
                    continue

                observation = _measurement_observation(frozen_observation)
                peer_snapshots = frozen_input.get("peer_observations")
                if not isinstance(peer_snapshots, list):
                    raise ValueError("frozen peer Observation catalog is invalid")
                peer_key = sha256_json(peer_snapshots)
                cached_peers = peer_cache.get(peer_key)
                if cached_peers is None:
                    peers = [
                        _peer_observation(value)
                        for value in peer_snapshots
                        if isinstance(value, Mapping)
                    ]
                    canonical_peer_snapshots = peer_snapshots
                    peer_cache[peer_key] = (peers, canonical_peer_snapshots)
                else:
                    peers, canonical_peer_snapshots = cached_peers
                if len(peers) != len(peer_snapshots):
                    raise ValueError("frozen peer Observation catalog is malformed")
                frozen_lineage = frozen_input.get("lineage")
                if not isinstance(frozen_lineage, Mapping):
                    raise ValueError("frozen lineage snapshot is invalid")
                source, raw_revision_id, raw_content_hash = _load_raw_source(
                    raw_connection,
                    frozen_observation,
                    frozen_lineage,
                )
                replay_input = dict(frozen_input)
                replay_input["peer_observations"] = canonical_peer_snapshots
                report = current_engine.recalibrate_frozen_snapshot(
                    observation,
                    peers,
                    [source],
                    frozen_input_snapshot=replay_input,
                    expected_input_hash=str(payload["calculation_input_hash"]),
                    valid_from=str(payload["valid_from"]),
                    valid_until=str(payload["valid_until"]),
                    omission_receipts=payload["omission_receipts"],
                )
                durable_payload = calibration_record_payload(observation, report)
                validate_cognitive_state_payload("calibration_record", durable_payload)
                replays.append(
                    CalibrationReplay(
                        object_id=revision.object_id,
                        old_revision_id=revision.revision_id,
                        old_payload_hash=revision.payload_hash,
                        observation_row_hash=row_hash,
                        raw_revision_id=raw_revision_id,
                        raw_content_hash=raw_content_hash,
                        expected_input_hash=report.calculation_input_hash,
                        expected_payload_hash=sha256_json(durable_payload),
                        dimension=observation.dimension.value,
                        observation=observation,
                        report=report,
                    )
                )
            except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                blocked.append(_blocked(revision.object_id, revision.revision_id, exc))

    current_by_object = {
        str(value["object_id"]): value for value in current_manifests
    }
    command_closures: list[CalibrationCommandClosure] = []
    with _connect_read_only(paths.state_path) as state_connection:
        pending_historical = state_connection.execute(
            """
            SELECT o.command_id, o.consumer_id, old.object_id,
                   old.revision_id AS old_revision_id,
                   old.payload_hash AS old_payload_hash,
                   current.revision_id AS current_revision_id,
                   current.payload_hash AS current_payload_hash
            FROM cognitive_state_outbox AS o
            JOIN cognitive_state_revisions AS old ON old.revision_id=o.revision_id
            LEFT JOIN cognitive_state_effect_receipts AS effect
              ON effect.command_id=o.command_id
            JOIN cognitive_state_heads AS head
              ON head.object_type=old.object_type AND head.object_id=old.object_id
            JOIN cognitive_state_revisions AS current
              ON current.revision_id=head.revision_id
            WHERE old.object_type='calibration_record'
              AND o.command_type='project_calibration_record'
              AND effect.command_id IS NULL
              AND old.revision_id<>current.revision_id
            ORDER BY o.command_id
            """
        ).fetchall()
    for row in pending_historical:
        current_manifest = current_by_object.get(str(row["object_id"]))
        if (
            current_manifest is None
            or current_manifest.get("revision_id") != row["current_revision_id"]
            or current_manifest.get("payload_hash") != row["current_payload_hash"]
        ):
            blocked.append(
                {
                    "object_id": str(row["object_id"]),
                    "revision_id": str(row["old_revision_id"]),
                    "reason": "pending historical command lacks a verified current successor",
                }
            )
            continue
        command_closures.append(
            CalibrationCommandClosure(
                command_id=str(row["command_id"]),
                consumer_id=str(row["consumer_id"]),
                object_id=str(row["object_id"]),
                old_revision_id=str(row["old_revision_id"]),
                old_payload_hash=str(row["old_payload_hash"]),
                current_revision_id=str(row["current_revision_id"]),
                current_payload_hash=str(row["current_payload_hash"]),
            )
        )

    replays.sort(key=lambda value: value.object_id)
    retirements.sort(key=lambda value: value.object_id)
    current_manifests.sort(key=lambda value: str(value["object_id"]))
    blocked.sort(key=lambda value: (value["object_id"], value["revision_id"]))
    command_closures.sort(key=lambda value: value.command_id)
    replay_manifests = [value.manifest() for value in replays]
    retirement_manifests = [value.manifest() for value in retirements]
    command_closure_manifests = [value.manifest() for value in command_closures]
    object_manifest_hash, inventory_hash = finalize_plan_hashes(
        validator_spec_hash=current_engine.spec_hash,
        replay_manifests=replay_manifests,
        retirement_manifests=retirement_manifests,
        command_closure_manifests=command_closure_manifests,
        current_manifests=current_manifests,
        blocked=blocked,
    )
    return CalibrationReconciliationPlan(
        paths=paths,
        validator_spec_hash=current_engine.spec_hash,
        current_count=len(revisions),
        replays=tuple(replays),
        retirements=tuple(retirements),
        command_closures=tuple(command_closures),
        current_object_manifests=tuple(current_manifests),
        blocked=tuple(blocked),
        object_manifest_hash=object_manifest_hash,
        inventory_hash=inventory_hash,
    )


__all__ = ["build_calibration_reconciliation_plan"]
