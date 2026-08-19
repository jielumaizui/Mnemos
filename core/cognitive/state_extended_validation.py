"""Belief, calibration, and cognition-episode payload validation."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from core.cognition_episode_contract import (
    COGNITION_EPISODE_FIELDS,
    COGNITION_EPISODE_SCHEMA_VERSION,
    COGNITION_EXTRACTION_CONTEXT_VERSION,
    LEGACY_COGNITION_EPISODE_SCHEMA_VERSION,
    SUPPORTED_COGNITION_EPISODE_SCHEMA_VERSIONS,
    VISIBLE_INPUT_LOSS_CONTRACT,
)
from core.cognitive.calibration_math import recompute_posterior, validator_input_hash
from core.cognitive.state_contract_schema import (
    COGNITIVE_OBJECT_SCHEMA_VERSIONS,
    _OMISSION_ID_PATTERN,
    _SHA256_PATTERN,
)
from core.cognitive.state_validation_primitives import (
    _finite_float,
    _is_non_negative_int,
    _positive_finite,
    _parse_timestamp,
    _required_sha256,
    _required_text,
    _validate_source_span_id,
    sha256_json,
)
from core.privacy.content_redaction import REDACTION_POLICY


def _validate_belief_revision_payload(payload: Mapping[str, Any]) -> None:
    if payload["schema_version"] != COGNITIVE_OBJECT_SCHEMA_VERSIONS["belief_revision"]:
        raise ValueError("belief_revision payload schema_version mismatch")
    if not re.fullmatch(r"belief-[0-9a-f]{32}", str(payload["belief_id"])):
        raise ValueError("belief_revision.belief_id is invalid")
    if not re.fullmatch(r"claim-[0-9a-f]{32}", str(payload["claim_id"])):
        raise ValueError("belief_revision.claim_id is invalid")
    if payload["claim_kind"] not in {
        "fact",
        "hypothesis",
        "preference",
        "policy",
        "decision_assumption",
    }:
        raise ValueError("belief_revision.claim_kind is invalid")
    stance = str(payload["stance"])
    if stance not in {"supported", "refuted", "disputed", "unknown", "deprecated"}:
        raise ValueError("belief_revision.stance is invalid")

    evidence_sets: dict[str, tuple[str, ...]] = {}
    for field_name in (
        "supporting_evidence",
        "opposing_evidence",
        "withdrawn_evidence",
        "confidence_evidence",
        "invalidation_conditions",
    ):
        values = tuple(str(value) for value in payload[field_name])
        if values != tuple(sorted(set(values))):
            raise ValueError(f"belief_revision.{field_name} must be sorted and unique")
        if any(not value for value in values):
            raise ValueError(f"belief_revision.{field_name} contains a blank item")
        evidence_sets[field_name] = values
    supporting = set(evidence_sets["supporting_evidence"])
    opposing = set(evidence_sets["opposing_evidence"])
    withdrawn = set(evidence_sets["withdrawn_evidence"])
    if supporting & opposing:
        raise ValueError("belief_revision active evidence cannot support and oppose")
    if withdrawn & (supporting | opposing):
        raise ValueError("belief_revision withdrawn evidence cannot remain active")
    derived_stance = (
        "disputed"
        if supporting and opposing
        else "supported" if supporting else "refuted" if opposing else "unknown"
    )
    if stance != "deprecated" and stance != derived_stance:
        raise ValueError("belief_revision stance does not match evidence")
    if stance in {"supported", "refuted", "disputed"} and not (supporting or opposing):
        raise ValueError("active belief revision requires evidence")

    confidence_method = str(payload["confidence_method"])
    confidence = payload["confidence"]
    confidence_evidence = evidence_sets["confidence_evidence"]
    if confidence_method == "unscored":
        if confidence is not None or confidence_evidence:
            raise ValueError("unscored belief cannot carry numerical confidence")
    else:
        if confidence is None or not confidence_evidence:
            raise ValueError("scored belief requires confidence evidence")
        numeric_confidence = float(confidence)
        if not 0.0 <= numeric_confidence <= 1.0:
            raise ValueError("belief_revision.confidence must be between 0 and 1")

    uncertainty = payload["uncertainty"]
    if not isinstance(uncertainty, Mapping) or set(uncertainty) != {
        "status",
        "reasons",
    }:
        raise ValueError("belief_revision.uncertainty is invalid")
    reasons = tuple(str(value) for value in uncertainty["reasons"])
    if reasons != tuple(sorted(set(reasons))) or any(not value for value in reasons):
        raise ValueError("belief_revision uncertainty reasons are invalid")
    expected_uncertainty = "uncertain" if reasons else "bounded"
    if uncertainty["status"] != expected_uncertainty:
        raise ValueError("belief_revision uncertainty status mismatch")

    valid_from = _parse_timestamp(payload["valid_from"], "belief_revision.valid_from")
    valid_until_raw = str(payload["valid_until"] or "")
    if valid_until_raw:
        valid_until = _parse_timestamp(valid_until_raw, "belief_revision.valid_until")
        if valid_until <= valid_from:
            raise ValueError("belief_revision valid_until must follow valid_from")
    if not evidence_sets["invalidation_conditions"]:
        raise ValueError("belief_revision invalidation_conditions must be non-empty")

    admission_refs = payload["admission_refs"]
    if not isinstance(admission_refs, Mapping) or set(admission_refs) != {
        "proposal_id",
        "journal_id",
        "projection_effect_id",
    }:
        raise ValueError("belief_revision.admission_refs is invalid")
    if not re.fullmatch(
        r"belief-effect-[0-9a-f]{32}",
        str(admission_refs["projection_effect_id"]),
    ):
        raise ValueError("belief_revision projection effect identity is invalid")
    for field_name in ("proposal_id", "journal_id"):
        if not isinstance(admission_refs[field_name], str):
            raise ValueError(f"belief_revision {field_name} must be text")
    for field_name in ("supersedes_revision_id", "correction_of_revision_id"):
        value = str(payload[field_name] or "")
        if value and not value.startswith("cogrev-"):
            raise ValueError(f"belief_revision {field_name} is invalid")


def _validate_calibration_record_payload(payload: Mapping[str, Any]) -> None:
    if payload["schema_version"] != COGNITIVE_OBJECT_SCHEMA_VERSIONS["calibration_record"]:
        raise ValueError("calibration_record payload schema_version mismatch")
    prior = float(payload["prior"])
    posterior = float(payload["posterior"])
    if not 0.0 <= prior <= 1.0 or not 0.0 <= posterior <= 1.0:
        raise ValueError("calibration_record prior/posterior must be between 0 and 1")
    if payload["overall_verdict"] not in {"confirmed", "questionable", "refuted"}:
        raise ValueError("calibration_record overall_verdict is invalid")
    if payload["validator_version"] != "mnemos.observation_calibration_spec.v1":
        raise ValueError("calibration_record validator version mismatch")
    calculation_input_hash = _required_sha256(
        payload["calculation_input_hash"],
        "calibration_record.calculation_input_hash",
    )
    validator_spec_hash = _required_sha256(
        payload["validator_spec_hash"],
        "calibration_record.validator_spec_hash",
    )
    if sha256_json(payload["input_snapshot"]) != calculation_input_hash:
        raise ValueError("calibration_record calculation input hash mismatch")
    input_snapshot = payload["input_snapshot"]
    if not isinstance(input_snapshot, Mapping):
        raise ValueError("calibration_record input snapshot must be an object")
    validator_spec = input_snapshot.get("validator_spec")
    lineage_snapshot = input_snapshot.get("lineage")
    observation_snapshot = input_snapshot.get("observation")
    if (
        not isinstance(validator_spec, Mapping)
        or sha256_json(validator_spec) != validator_spec_hash
    ):
        raise ValueError("calibration_record validator spec hash mismatch")
    if validator_spec.get("schema_version") != payload["validator_version"]:
        raise ValueError("calibration_record validator spec version mismatch")
    if validator_spec.get("combiner") != "weighted_evidence_shrinkage_v1":
        raise ValueError("calibration_record combiner is invalid")
    prior_weight = _positive_finite(
        validator_spec.get("prior_weight"),
        "calibration_record validator prior_weight",
    )
    implementation_hashes = validator_spec.get("implementation_hashes")
    if (
        not isinstance(implementation_hashes, Mapping)
        or set(implementation_hashes)
        != {"calibration_engine", "calibration_math_module", "lineage_module"}
        or any(
            not _SHA256_PATTERN.fullmatch(str(value)) for value in implementation_hashes.values()
        )
    ):
        raise ValueError("calibration_record implementation hashes are invalid")
    privacy = input_snapshot.get("privacy_redaction")
    privacy_counts = privacy.get("counts") if isinstance(privacy, Mapping) else None
    if (
        not isinstance(privacy, Mapping)
        or privacy.get("policy") != REDACTION_POLICY
        or not isinstance(privacy_counts, (list, tuple))
        or any(
            not isinstance(value, Mapping)
            or not str(value.get("type") or "")
            or not _is_non_negative_int(value.get("count"))
            for value in privacy_counts
        )
    ):
        raise ValueError("calibration_record privacy redaction metadata is invalid")
    if not isinstance(lineage_snapshot, Mapping) or sha256_json(
        lineage_snapshot
    ) != input_snapshot.get("lineage_snapshot_hash"):
        raise ValueError("calibration_record lineage snapshot hash mismatch")
    if (
        not isinstance(observation_snapshot, Mapping)
        or observation_snapshot.get("observation_id") != payload["observation_id"]
        or _finite_float(
            observation_snapshot.get("base_confidence"),
            "calibration_record base confidence",
        )
        != prior
        or observation_snapshot.get("base_measurement_status") != "verified"
        or not _SHA256_PATTERN.fullmatch(str(observation_snapshot.get("measurement_hash") or ""))
    ):
        raise ValueError("calibration_record observation snapshot mismatch")
    peer_observations = input_snapshot.get("peer_observations")
    if not isinstance(peer_observations, (list, tuple)) or any(
        not isinstance(value, Mapping)
        or not _SHA256_PATTERN.fullmatch(str(value.get("peer_identity") or ""))
        or not _SHA256_PATTERN.fullmatch(str(value.get("measurement_hash") or ""))
        for value in peer_observations
    ):
        raise ValueError("calibration_record peer Observation snapshot is invalid")
    peer_snapshot_order = [
        (str(value["measurement_hash"]), str(value["peer_identity"])) for value in peer_observations
    ]
    if peer_snapshot_order != sorted(peer_snapshot_order):
        raise ValueError("calibration_record peer Observation snapshot is not canonical")
    code_hashes = payload["validator_code_hashes"]
    if (
        not isinstance(code_hashes, Mapping)
        or not code_hashes
        or any(
            not str(name).strip() or not _SHA256_PATTERN.fullmatch(str(code_hash))
            for name, code_hash in code_hashes.items()
        )
    ):
        raise ValueError("calibration_record validator code hashes are invalid")
    if int(payload["derived_source_double_count"]) != 0:
        raise ValueError("calibration_record derived source double count must be zero")
    if int(payload["derived_members_deduplicated"]) < 0:
        raise ValueError("calibration_record derived dedup count is invalid")
    clusters = payload["independent_evidence_clusters"]
    cluster_ids: set[str] = set()
    for cluster in clusters:
        if not isinstance(cluster, Mapping):
            raise ValueError("calibration_record evidence cluster must be an object")
        cluster_id = _required_text(cluster.get("cluster_id"), "cluster_id")
        if cluster_id in cluster_ids:
            raise ValueError("calibration_record evidence clusters must be unique")
        cluster_ids.add(cluster_id)
        if not cluster.get("independent_eligible") or not cluster.get("lineage_roots"):
            raise ValueError("calibration_record evidence cluster lacks canonical lineage")
        root_hashes = cluster.get("lineage_root_hashes")
        if not isinstance(root_hashes, (list, tuple)) or any(
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or not _SHA256_PATTERN.fullmatch(str(value[1]))
            for value in root_hashes
        ):
            raise ValueError("calibration_record lineage root hashes are invalid")
        if len(root_hashes) != len(cluster["lineage_roots"]) or {
            str(value[0]) for value in root_hashes
        } != set(cluster["lineage_roots"]):
            raise ValueError("calibration_record lineage root hash catalog mismatch")
    ordered_cluster_ids = [str(cluster["cluster_id"]) for cluster in clusters]
    if ordered_cluster_ids != sorted(ordered_cluster_ids):
        raise ValueError("calibration_record evidence clusters are not canonical")
    for field_name in ("supporting_evidence", "counter_evidence"):
        values = tuple(str(value) for value in payload[field_name])
        if len(values) != len(set(values)) or any(value not in cluster_ids for value in values):
            raise ValueError(f"calibration_record {field_name} is outside the cluster catalog")
    if set(payload["supporting_evidence"]) & set(payload["counter_evidence"]):
        raise ValueError("calibration_record support and counter evidence overlap")
    spec_validators = validator_spec.get("validators", ())
    if not isinstance(spec_validators, (list, tuple)):
        raise ValueError("calibration_record validator spec catalog is invalid")
    validator_weights = {}
    for value in spec_validators:
        if not isinstance(value, Mapping):
            continue
        name = str(value.get("name") or "")
        validator_weights[name] = _positive_finite(
            value.get("weight"),
            f"calibration_record validator weight {name}",
        )
    spec_code_hashes = {
        str(value.get("name") or ""): str(value.get("code_hash") or "")
        for value in spec_validators
        if isinstance(value, Mapping)
    }
    if len(validator_weights) != len(spec_validators) or spec_code_hashes != {
        str(key): str(value) for key, value in code_hashes.items()
    }:
        raise ValueError("calibration_record validator spec/code catalog mismatch")
    validation_names: set[str] = set()
    for validation in payload["validations"]:
        if not isinstance(validation, Mapping):
            raise ValueError("calibration_record validation must be an object")
        _required_text(validation.get("validator_name"), "validator_name")
        score = _finite_float(
            validation.get("score"),
            "calibration_record validation score",
        )
        if not 0.0 <= score <= 1.0:
            raise ValueError("calibration_record validation score is invalid")
        if validation.get("verdict") not in {
            "confirmed",
            "questionable",
            "refuted",
            "inconclusive",
        }:
            raise ValueError("calibration_record validation verdict is invalid")
        validator_name = str(validation["validator_name"])
        if validator_name in validation_names:
            raise ValueError("calibration_record validator result is duplicated")
        validation_names.add(validator_name)
        if validator_name not in code_hashes or validator_name not in validator_weights:
            raise ValueError("calibration_record validator is outside the spec")
        if (
            _finite_float(
                validation.get("weight"),
                "calibration_record validation weight",
            )
            != validator_weights[validator_name]
        ):
            raise ValueError("calibration_record validator weight mismatch")
        _required_text(validation.get("reason"), "calibration_record validation reason")
        expected_input_hash = validator_input_hash(
            calculation_input_hash=calculation_input_hash,
            validator_name=validator_name,
            validator_code_hash=str(code_hashes[validator_name]),
        )
        if validation.get("input_hash") != expected_input_hash:
            raise ValueError("calibration_record validator input hash mismatch")
        for ref_field in ("supporting_cluster_ids", "counter_cluster_ids"):
            refs = [str(value) for value in validation.get(ref_field, ())]
            if refs != sorted(set(refs)):
                raise ValueError("calibration_record validator refs are not canonical")
            if any(value not in cluster_ids for value in refs):
                raise ValueError("calibration_record validator ref is outside the cluster catalog")
    if validation_names != set(validator_weights):
        raise ValueError("calibration_record validator results are incomplete")
    expected_supporting = sorted(
        {
            str(value)
            for validation in payload["validations"]
            for value in validation.get("supporting_cluster_ids", ())
        }
    )
    expected_counter = sorted(
        {
            str(value)
            for validation in payload["validations"]
            for value in validation.get("counter_cluster_ids", ())
        }
    )
    if list(payload["supporting_evidence"]) != expected_supporting:
        raise ValueError("calibration_record supporting evidence aggregate mismatch")
    if list(payload["counter_evidence"]) != expected_counter:
        raise ValueError("calibration_record counter evidence aggregate mismatch")
    source_span_ids = [_validate_source_span_id(value) for value in payload["source_span_ids"]]
    if source_span_ids != sorted(set(source_span_ids)):
        raise ValueError("calibration_record source spans are not canonical")
    recomputed = recompute_posterior(
        prior,
        payload["validations"],
        prior_weight=prior_weight,
    )
    if abs(recomputed - posterior) > 1e-9:
        raise ValueError("calibration_record posterior is not replayable")
    receipts = payload["omission_receipts"]
    receipt_ids = [
        _required_text(receipt.get("receipt_id"), "receipt_id")
        for receipt in receipts
        if isinstance(receipt, Mapping)
    ]
    if len(receipt_ids) != len(receipts) or len(receipt_ids) != len(set(receipt_ids)):
        raise ValueError("calibration_record omission receipts are invalid")
    if any(not _OMISSION_ID_PATTERN.fullmatch(value) for value in receipt_ids):
        raise ValueError("calibration_record omission receipt identity is malformed")
    receipt_targets = {str(receipt.get("target") or "") for receipt in receipts}
    if receipt_targets != {"evidence_snippets", "source_paths", "source_span_ids"}:
        raise ValueError("calibration_record omission receipt targets are incomplete")
    for receipt in receipts:
        target = str(receipt["target"])
        total = int(receipt.get("total_count"))
        displayed = int(receipt.get("displayed_count"))
        omitted = int(receipt.get("omitted_count"))
        limit = 5 if target == "evidence_snippets" else 20
        if (
            total < 0
            or displayed != min(total, limit)
            or omitted != total - displayed
            or not _SHA256_PATTERN.fullmatch(str(receipt.get("omitted_hash") or ""))
        ):
            raise ValueError("calibration_record omission receipt counts are invalid")
        identity = dict(receipt)
        receipt_id = str(identity.pop("receipt_id"))
        expected_id = "omission:" + sha256_json(identity).split(":", 1)[1][:32]
        if receipt_id != expected_id:
            raise ValueError("calibration_record omission receipt identity mismatch")
        if target == "source_span_ids":
            expected_omitted_hash = sha256_json(list(payload["source_span_ids"])[limit:])
            if receipt["omitted_hash"] != expected_omitted_hash:
                raise ValueError("calibration_record source span omission hash mismatch")


def _validate_cognition_episode_payload(payload: Mapping[str, Any]) -> None:
    schema_version = str(payload["schema_version"])
    if schema_version not in SUPPORTED_COGNITION_EPISODE_SCHEMA_VERSIONS:
        raise ValueError("cognition_episode payload schema_version mismatch")
    if payload["acl"] != "local_user":
        raise ValueError("cognition_episode.acl must be local_user")
    if payload["loss_contract"] != VISIBLE_INPUT_LOSS_CONTRACT:
        raise ValueError("cognition_episode loss contract mismatch")
    if not payload["source_event_ids"] or not payload["source_spans"]:
        raise ValueError("cognition_episode requires source events and exact spans")
    authority_catalog = payload["source_authority_catalog"]
    if not isinstance(authority_catalog, Mapping) or not isinstance(
        authority_catalog.get("entries"), (list, tuple)
    ):
        raise ValueError("cognition_episode source authority catalog is invalid")
    authority_entries = {
        str(entry.get("source_authority_id") or ""): entry
        for entry in authority_catalog["entries"]
        if isinstance(entry, Mapping) and entry.get("source_authority_id")
    }
    for span in payload["source_spans"]:
        if not isinstance(span, Mapping) or span.get("span_status") != "exact":
            raise ValueError("cognition_episode source spans must be exact")
        try:
            span_start = int(span.get("span_start", -1))
            span_end = int(span.get("span_end", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError("cognition_episode source span bounds are invalid") from exc
        if (
            not span.get("revision_id")
            or span.get("revision_id") not in payload["source_event_ids"]
            or not span.get("source_revision_sha256")
            or not span.get("content_sha256")
            or not span.get("role")
            or span_start < 0
            or span_end <= span_start
        ):
            raise ValueError("cognition_episode source span identity is incomplete")
        authority = authority_entries.get(str(span.get("source_authority_id") or ""))
        if authority is None or any(
            authority.get(authority_key) != span.get(span_key)
            for authority_key, span_key in (
                ("source_event_id", "revision_id"),
                ("role", "role"),
                ("span_start", "span_start"),
                ("span_end", "span_end"),
                ("span_status", "span_status"),
                ("content_sha256", "content_sha256"),
                ("source_revision_sha256", "source_revision_sha256"),
            )
        ):
            raise ValueError("cognition_episode source span/catalog mismatch")
    context_payload = {
        "schema_version": COGNITION_EXTRACTION_CONTEXT_VERSION,
        "source_agent": payload["source_agent"],
        "source_session_id": payload["source_session_id"],
        "source_event_ids": list(payload["source_event_ids"]),
        "raw_completeness": payload["raw_completeness"],
        "loss_contract": payload["loss_contract"],
        "source_spans": list(payload["source_spans"]),
        "artifact_catalog_hash": payload["artifact_catalog_hash"],
        "source_authority_catalog_hash": payload["source_authority_catalog_hash"],
        "acl": payload["acl"],
        "access_control": payload["access_control"],
        "purpose": payload["purpose"],
        "retention_policy": payload["retention_policy"],
    }
    if sha256_json(context_payload) != payload["cognition_context_hash"]:
        raise ValueError("cognition_episode context hash mismatch")
    catalog_claim_ids: set[str] | None = None
    if schema_version == COGNITION_EPISODE_SCHEMA_VERSION:
        catalog_claim_ids = _validate_cognition_episode_claim_catalog(payload, authority_entries)
        _validate_cognition_episode_behavior_intent(payload, authority_entries)
    elif schema_version != LEGACY_COGNITION_EPISODE_SCHEMA_VERSION:
        raise ValueError("cognition_episode payload schema_version mismatch")

    seen_entry_ids: set[str] = set()
    referenced_claim_ids: set[str] = set()
    for field_name in COGNITION_EPISODE_FIELDS:
        entries = payload[field_name]
        if not entries:
            raise ValueError(f"cognition_episode.{field_name} must be non-empty")
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError(f"cognition_episode.{field_name} entry must be an object")
            entry_id = _required_text(entry.get("entry_id"), "entry_id")
            if entry_id in seen_entry_ids:
                raise ValueError("cognition_episode entry_id must be unique")
            seen_entry_ids.add(entry_id)
            status = str(entry.get("status") or "")
            evidence = entry.get("evidence_refs")
            entry_claim_ids = entry.get("claim_ids")
            if not isinstance(evidence, (list, tuple)) or not isinstance(
                entry_claim_ids, (list, tuple)
            ):
                raise ValueError(
                    f"cognition_episode.{field_name} evidence_refs/claim_ids must be sequences"
                )
            if status == "known":
                _required_text(entry.get("value"), f"cognition_episode.{field_name}.value")
                if not evidence or not entry_claim_ids:
                    raise ValueError(
                        f"cognition_episode.{field_name} known entry requires evidence and claims"
                    )
                for evidence_item in evidence:
                    _validate_cognition_episode_evidence(
                        evidence_item,
                        payload["source_event_ids"],
                        authority_entries,
                    )
                normalized_claim_ids = {str(value) for value in entry_claim_ids}
                if any(not value for value in normalized_claim_ids):
                    raise ValueError(
                        f"cognition_episode.{field_name} claim_ids contain a blank value"
                    )
                if catalog_claim_ids is not None and not normalized_claim_ids.issubset(
                    catalog_claim_ids
                ):
                    raise ValueError(f"cognition_episode.{field_name} references an unknown claim")
                referenced_claim_ids.update(normalized_claim_ids)
            elif status in {"unknown", "not_applicable"}:
                _required_text(entry.get("reason"), f"cognition_episode.{field_name}.reason")
                if evidence or entry_claim_ids or entry.get("value"):
                    raise ValueError(
                        f"cognition_episode.{field_name} non-known entry carries an assertion"
                    )
            else:
                raise ValueError(f"cognition_episode.{field_name}.status is invalid")
    if catalog_claim_ids is not None and referenced_claim_ids != catalog_claim_ids:
        raise ValueError("cognition_episode claim catalog mapping is incomplete")


_CLAIM_TYPES = frozenset(
    {
        "technical_fact",
        "preference",
        "procedure",
        "decision",
        "constraint",
        "pattern",
        "anti_pattern",
        "entity",
        "relationship",
        "open_question",
        "meta",
    }
)
_RELATION_TYPES = frozenset(
    {
        "new",
        "same",
        "extends",
        "refines",
        "specializes",
        "example",
        "related",
        "contradicts",
        "supersedes",
    }
)
_RECOMMENDED_ACTIONS = frozenset(
    {
        "create_page",
        "merge_into_page",
        "update_page",
        "route_to_dispute",
        "record_reinforcement",
        "skip",
    }
)


def _validate_cognition_episode_claim_catalog(
    payload: Mapping[str, Any],
    authority_entries: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    claims = payload["claims"]
    if not isinstance(claims, (list, tuple)) or not claims:
        raise ValueError("cognition_episode claims must be a non-empty sequence")
    if payload["claim_catalog_hash"] != sha256_json(list(claims)):
        raise ValueError("cognition_episode claim catalog hash mismatch")
    claim_ids: set[str] = set()
    source_event_ids = payload["source_event_ids"]
    for claim in claims:
        if not isinstance(claim, Mapping):
            raise ValueError("cognition_episode claim must be an object")
        claim_id = _required_text(claim.get("claim_id"), "claim_id")
        if claim_id in claim_ids:
            raise ValueError("cognition_episode claim_id must be unique")
        claim_ids.add(claim_id)
        _required_text(claim.get("claim_text"), "claim_text")
        if claim.get("claim_type") not in _CLAIM_TYPES:
            raise ValueError("cognition_episode claim_type is invalid")
        scope = claim.get("scope")
        if not isinstance(scope, Mapping):
            raise ValueError("cognition_episode claim scope is invalid")
        _required_text(scope.get("domain"), "claim scope domain")
        for field_name in ("applies_to", "not_applies_to"):
            values = scope.get(field_name, [])
            if not isinstance(values, (list, tuple)) or any(
                not isinstance(value, str) for value in values
            ):
                raise ValueError(f"cognition_episode claim scope {field_name} is invalid")
        evidence = claim.get("evidence")
        if not isinstance(evidence, (list, tuple)) or not evidence:
            raise ValueError("cognition_episode claim evidence is required")
        for evidence_item in evidence:
            _validate_cognition_episode_evidence(
                evidence_item,
                source_event_ids,
                authority_entries,
            )
        relation = claim.get("relation_to_existing")
        if not isinstance(relation, Mapping) or relation.get("type") not in _RELATION_TYPES:
            raise ValueError("cognition_episode claim relation is invalid")
        if claim.get("recommended_action") not in _RECOMMENDED_ACTIONS:
            raise ValueError("cognition_episode claim recommended_action is invalid")
        confidence = _finite_float(claim.get("confidence"), "claim confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("cognition_episode claim confidence is invalid")
    return claim_ids


def _validate_cognition_episode_behavior_intent(
    payload: Mapping[str, Any],
    authority_entries: Mapping[str, Mapping[str, Any]],
) -> None:
    behavior = payload["user_behavior_intent"]
    if not isinstance(behavior, Mapping):
        raise ValueError("cognition_episode user_behavior_intent is invalid")
    for field_name in (
        "content_source",
        "user_intent_signal",
        "intent_hypothesis",
        "intent_status",
        "behavior_summary",
    ):
        _required_text(behavior.get(field_name), f"user_behavior_intent.{field_name}")
    confidence = _finite_float(
        behavior.get("intent_confidence"),
        "user_behavior_intent.intent_confidence",
    )
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("cognition_episode behavior intent confidence is invalid")
    for field_name, required in (
        ("intent_evidence", True),
        ("intent_verification_events", False),
    ):
        evidence = behavior.get(field_name)
        if not isinstance(evidence, (list, tuple)) or (required and not evidence):
            raise ValueError(f"cognition_episode behavior {field_name} is invalid")
        for evidence_item in evidence:
            _validate_cognition_episode_evidence(
                evidence_item,
                payload["source_event_ids"],
                authority_entries,
            )


def _validate_cognition_episode_evidence(
    evidence: Any,
    source_event_ids: Sequence[Any],
    authority_entries: Mapping[str, Mapping[str, Any]],
) -> None:
    if not isinstance(evidence, Mapping):
        raise ValueError("cognition_episode evidence must be an object")
    source_event_id = str(evidence.get("source_event_id") or "")
    authority_id = str(evidence.get("source_authority_id") or "")
    authority = authority_entries.get(authority_id)
    if not source_event_id or source_event_id not in source_event_ids or authority is None:
        raise ValueError("cognition_episode evidence is outside the source catalog")
    expected = {
        "source_event_id": source_event_id,
        "role": evidence.get("authority_role"),
        "span_start": evidence.get("authority_span_start"),
        "span_end": evidence.get("authority_span_end"),
        "span_status": evidence.get("authority_span_status"),
        "content_sha256": evidence.get("authority_content_sha256"),
        "source_revision_sha256": evidence.get("authority_source_revision_sha256"),
    }
    if any(authority.get(key) != value for key, value in expected.items()):
        raise ValueError("cognition_episode evidence/source catalog mismatch")
