"""Independent DecisionTrace bundle and prediction verification."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from core.cognitive.decision_snapshot_access import (
    DECISION_SNAPSHOT_OUTPUT_PURPOSE,
    DECISION_SNAPSHOT_SOURCE_PURPOSE_CONTRACT_HASH,
    DECISION_SNAPSHOT_SOURCE_PURPOSE_SCHEMA_VERSION,
    DECISION_SNAPSHOT_SOURCE_PURPOSES,
)
from core.cognitive.state_contract import (
    COGNITIVE_OBJECT_SCHEMA_VERSIONS,
    FIXED_VALUE_PRECEDENCE,
    VALUE_PRECEDENCE_CONTRACT,
    CognitiveStateRevision,
    sha256_json,
)
from core.cognitive.state_store import CognitiveStateStore

from core.cognitive.decision_trace_contracts import (
    MATERIAL_ACTION_COMMAND_TYPE,
    _contains_private_reasoning,
    _digest,
    _mapping_sequence,
    _required,
    _revision_from_row,
    _sha256,
)
from core.cognitive.decision_trace_payloads import (
    _historical_snapshot_revision_entry,
    _snapshot_revision_entry,
    _verify_revision_payload_hash,
    _verify_snapshot_hash,
)


def _normalize_material_prediction_refs(value: Any) -> tuple[dict[str, str], ...]:
    rows = _mapping_sequence(value, "material prediction refs")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if set(row) != {
            "prediction_id",
            "prediction_plan_hash",
            "prediction_revision_id",
            "prediction_revision_hash",
        }:
            raise RuntimeError("material-action prediction ref is malformed")
        prediction_id = _required(row.get("prediction_id"), "prediction_id")
        if prediction_id in seen:
            raise RuntimeError("material-action prediction refs are duplicated")
        seen.add(prediction_id)
        normalized.append(
            {
                "prediction_id": prediction_id,
                "prediction_plan_hash": _sha256(
                    row.get("prediction_plan_hash"),
                    "prediction_plan_hash",
                ),
                "prediction_revision_id": _required(
                    row.get("prediction_revision_id"),
                    "prediction_revision_id",
                ),
                "prediction_revision_hash": _sha256(
                    row.get("prediction_revision_hash"),
                    "prediction_revision_hash",
                ),
            }
        )
    return tuple(sorted(normalized, key=lambda item: item["prediction_id"]))


def _verify_prediction_refs(
    state_store: CognitiveStateStore,
    decision: CognitiveStateRevision,
) -> tuple[str, ...]:
    refs = tuple(
        dict(value)
        for value in decision.payload.get("prediction_refs", ())
        if isinstance(value, Mapping)
    )
    if len(refs) != len(decision.payload.get("prediction_refs", ())):
        raise RuntimeError("DecisionTrace prediction refs are malformed")
    if not refs:
        return ()
    with state_store._connect(read_only=True) as conn:  # noqa: SLF001
        rows = conn.execute(
            """
            SELECT * FROM cognitive_state_revisions
            WHERE object_type='prediction_record' AND source_event_id=?
            ORDER BY object_id, revision_id
            """,
            (decision.source_event_id,),
        ).fetchall()
    revisions = tuple(_revision_from_row(row) for row in rows)
    if len(revisions) != len(refs):
        raise RuntimeError("DecisionTrace prediction revision cardinality mismatch")
    by_id = {revision.object_id: revision for revision in revisions}
    resolved: list[str] = []
    for ref in refs:
        revision = by_id.get(str(ref.get("prediction_id") or ""))
        if (
            revision is None
            or revision.source_event_id != decision.source_event_id
            or revision.payload.get("decision_ref", {}).get("revision_id") != decision.revision_id
            or revision.payload.get("decision_ref", {}).get("revision_hash")
            != decision.payload_hash
            or revision.payload.get("prediction_plan_hash") != ref.get("prediction_plan_hash")
        ):
            raise RuntimeError("DecisionTrace prediction binding failed")
        resolved.append(revision.revision_id)
    return tuple(resolved)


def _verify_decision_supersessions(
    state_store: CognitiveStateStore,
    decision: CognitiveStateRevision,
) -> None:
    superseded = tuple(
        str(value)
        for value in decision.payload.get(
            "supersedes_decision_revision_ids",
            (),
        )
    )
    if not superseded:
        return
    current_actions = tuple(
        dict(value)
        for value in decision.payload.get("action_specs", ())
        if isinstance(value, Mapping)
    )
    with sqlite3.connect(
        f"file:{Path(state_store.db_path).resolve(strict=True)}?mode=ro",
        uri=True,
    ) as conn:
        conn.row_factory = sqlite3.Row
        for revision_id in superseded:
            prior = state_store.revision(revision_id)
            if (
                prior is None
                or prior.object_type != "decision_trace"
                or (prior.scope_type, prior.scope_id) != (decision.scope_type, decision.scope_id)
                or datetime.fromisoformat(prior.created_at)
                > datetime.fromisoformat(decision.created_at)
            ):
                raise RuntimeError("DecisionTrace supersedes an invalid or later decision revision")
            rows = conn.execute(
                """
                SELECT o.payload_json
                FROM cognitive_state_outbox AS o
                JOIN cognitive_state_effect_receipts AS r
                  ON r.command_id=o.command_id
                WHERE o.revision_id=? AND o.command_type=?
                  AND r.status='dead_letter'
                """,
                (revision_id, MATERIAL_ACTION_COMMAND_TYPE),
            ).fetchall()
            prior_actions = []
            for row in rows:
                try:
                    payload = json.loads(str(row["payload_json"]))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("superseded material command payload is malformed") from exc
                if isinstance(payload, Mapping):
                    prior_actions.append(payload)
            if not any(
                all(
                    str(prior_action.get(key) or "") == str(current_action.get(current_key) or "")
                    for key, current_key in (
                        ("owner", "owner"),
                        ("executor", "executor"),
                        ("action_type", "action_type"),
                        ("target_ref", "target_ref"),
                        ("input_hash", "input_hash"),
                    )
                )
                for prior_action in prior_actions
                for current_action in current_actions
            ):
                raise RuntimeError("DecisionTrace supersession lacks an exact dead-letter action")


def _verify_decision_bundle(
    state_store: CognitiveStateStore,
    *,
    decision: CognitiveStateRevision,
    snapshot: CognitiveStateRevision,
    value_context: CognitiveStateRevision,
) -> None:
    if decision.object_type != "decision_trace":
        raise RuntimeError("decision revision has the wrong object type")
    if snapshot.object_type != "cognitive_state_snapshot":
        raise RuntimeError("snapshot revision has the wrong object type")
    if value_context.object_type != "value_context":
        raise RuntimeError("ValueContext revision has the wrong object type")
    for revision in (decision, snapshot, value_context):
        _verify_revision_payload_hash(revision)
    _verify_snapshot_hash(snapshot)
    _verify_decision_supersessions(state_store, decision)
    if (
        len(
            {
                (decision.scope_type, decision.scope_id),
                (snapshot.scope_type, snapshot.scope_id),
                (value_context.scope_type, value_context.scope_id),
            }
        )
        != 1
    ):
        raise RuntimeError("decision bundle scope mismatch")

    decision_payload = decision.payload
    snapshot_payload = snapshot.payload
    value_payload = value_context.payload
    if (
        decision_payload.get("decision_id") != decision.object_id
        or decision_payload.get("snapshot_id") != snapshot.object_id
        or decision_payload.get("snapshot_revision_id") != snapshot.revision_id
        or decision_payload.get("snapshot_hash") != snapshot_payload.get("snapshot_hash")
        or decision_payload.get("value_context_id") != value_context.object_id
        or decision_payload.get("value_context_revision_id") != value_context.revision_id
        or decision_payload.get("value_context_hash") != value_context.payload_hash
        or snapshot_payload.get("value_context_revision_id") != value_context.revision_id
        or snapshot_payload.get("value_context_hash") != value_context.payload_hash
        or value_payload.get("value_context_id") != value_context.object_id
        or value_payload.get("supersedes_revision_id", "") != value_context.supersedes_revision_id
    ):
        raise RuntimeError("decision bundle canonical reference mismatch")
    if (
        value_payload.get("precedence_contract") != VALUE_PRECEDENCE_CONTRACT
        or tuple(value_payload.get("precedence", ())) != FIXED_VALUE_PRECEDENCE
    ):
        raise RuntimeError("ValueContext precedence contract mismatch")
    authority_catalog = value_payload.get("source_authority_catalog")
    if not isinstance(authority_catalog, Mapping):
        raise RuntimeError("ValueContext source authority catalog is unavailable")
    authority_catalog_hash = sha256_json(authority_catalog)
    if (
        value_payload.get("source_authority_catalog_hash") != authority_catalog_hash
        or snapshot_payload.get("source_authority_catalog_hash") != authority_catalog_hash
        or decision_payload.get("source_authority_catalog_hash") != authority_catalog_hash
    ):
        raise RuntimeError("decision bundle source authority catalog mismatch")
    authority_entries = {
        str(entry.get("source_authority_id") or ""): entry
        for entry in authority_catalog.get("entries", ())
        if isinstance(entry, Mapping)
    }
    if not authority_entries:
        raise RuntimeError("ValueContext source authority catalog is empty")

    values = value_payload.get("items")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
        raise RuntimeError("ValueContext items are unavailable")
    item_refs: list[str] = []
    item_by_ref: dict[str, Mapping[str, Any]] = {}
    for raw in values:
        if not isinstance(raw, Mapping):
            raise RuntimeError("ValueContext item is malformed")
        item = dict(raw)
        item_ref = str(item.pop("item_ref", ""))
        if item_ref != "value-" + _digest(item)[:32]:
            raise RuntimeError("ValueContext item ref mismatch")
        if str(item.get("category") or "") not in FIXED_VALUE_PRECEDENCE:
            raise RuntimeError("ValueContext item category mismatch")
        _sha256(item.get("source_content_hash"), "value source_content_hash")
        authority = authority_entries.get(str(item.get("source_authority_id") or ""))
        if (
            authority is None
            or not bool(authority.get("allows_cognitive_update"))
            or item.get("source_authority") != authority.get("source_authority")
            or item.get("source_id") != authority.get("source_event_id")
            or item.get("source_content_hash") != authority.get("content_sha256")
            or item.get("source_authority_id") not in item.get("evidence_refs", ())
        ):
            raise RuntimeError("ValueContext item authority binding mismatch")
        if item_ref in item_by_ref:
            raise RuntimeError("ValueContext item refs are not unique")
        item_refs.append(item_ref)
        item_by_ref[item_ref] = raw
    canonical_item_refs = sorted(item_refs)
    if list(value_payload.get("consumed_refs", ())) != canonical_item_refs or value_payload.get(
        "consumed_refs_hash"
    ) != sha256_json(canonical_item_refs):
        raise RuntimeError("ValueContext consumed-ref hash mismatch")
    for conflict in value_payload.get("conflicts", ()):
        if not isinstance(conflict, Mapping):
            raise RuntimeError("ValueContext conflict is malformed")
        winner = item_by_ref.get(str(conflict.get("winner_item_ref") or ""))
        loser = item_by_ref.get(str(conflict.get("loser_item_ref") or ""))
        if winner is None or loser is None:
            raise RuntimeError("ValueContext conflict ref is missing")
        if (
            FIXED_VALUE_PRECEDENCE.index(str(winner["category"]))
            >= (FIXED_VALUE_PRECEDENCE.index(str(loser["category"])))
            or conflict.get("disposition_code") != "higher_precedence_wins"
        ):
            raise RuntimeError("ValueContext conflict disposition mismatch")

    consumed_state = snapshot_payload.get("consumed_state")
    if not isinstance(consumed_state, Sequence) or isinstance(consumed_state, (str, bytes)):
        raise RuntimeError("snapshot consumed state is malformed")
    consumed_triples: list[dict[str, str]] = []
    belief_refs: list[str] = []
    policy_refs: list[str] = []
    snapshot_schema = str(snapshot_payload.get("schema_version") or "")
    typed_source_contract = (
        snapshot_schema == COGNITIVE_OBJECT_SCHEMA_VERSIONS["cognitive_state_snapshot"]
    )
    if typed_source_contract:
        source_completeness = snapshot_payload.get("source_completeness")
        if not isinstance(source_completeness, Mapping):
            raise RuntimeError("snapshot source completeness is malformed")
        expected_contract = {
            "schema_version": DECISION_SNAPSHOT_SOURCE_PURPOSE_SCHEMA_VERSION,
            "contract_hash": DECISION_SNAPSHOT_SOURCE_PURPOSE_CONTRACT_HASH,
            "output_purpose": DECISION_SNAPSHOT_OUTPUT_PURPOSE,
        }
        if source_completeness.get("contract") != expected_contract:
            raise RuntimeError("snapshot source-purpose contract mismatch")
        summaries = source_completeness.get("by_object_type")
        if not isinstance(summaries, Mapping) or set(summaries) != set(
            DECISION_SNAPSHOT_SOURCE_PURPOSES
        ):
            raise RuntimeError("snapshot source-purpose denominator mismatch")
        for object_type, purpose in DECISION_SNAPSHOT_SOURCE_PURPOSES.items():
            summary = summaries.get(object_type)
            if not isinstance(summary, Mapping) or summary.get("purpose") != purpose:
                raise RuntimeError("snapshot source-purpose summary mismatch")
    elif snapshot_schema != "mnemos.cognitive_state_snapshot.v1":
        raise RuntimeError("snapshot source-purpose schema is unsupported")
    for raw in consumed_state:
        if not isinstance(raw, Mapping):
            raise RuntimeError("snapshot consumed revision entry is malformed")
        revision_id = _required(raw.get("revision_id"), "consumed revision_id")
        linked = state_store.revision(revision_id)
        if linked is None:
            raise RuntimeError("snapshot consumed revision is unavailable")
        expected_purpose = DECISION_SNAPSHOT_SOURCE_PURPOSES.get(linked.object_type)
        expected_entry = _historical_snapshot_revision_entry(linked)
        if typed_source_contract:
            if expected_purpose is None:
                raise RuntimeError("snapshot consumed object type is not authorized")
            expected_entry = _snapshot_revision_entry(
                linked,
                source_read_purpose=expected_purpose,
                source_purpose_contract_hash=(DECISION_SNAPSHOT_SOURCE_PURPOSE_CONTRACT_HASH),
            )
        if dict(raw) != expected_entry:
            raise RuntimeError("snapshot consumed revision does not recompute")
        consumed_triples.append(
            {
                "object_type": linked.object_type,
                "object_id": linked.object_id,
                "revision_id": linked.revision_id,
            }
        )
        if linked.object_type == "belief_revision":
            belief_refs.append(linked.revision_id)
            if linked.payload.get("claim_kind") == "policy":
                policy_refs.append(linked.revision_id)
    if snapshot_payload.get("state_hash") != sha256_json(list(consumed_state)):
        raise RuntimeError("snapshot consumed-state hash mismatch")
    if list(snapshot_payload.get("head_preconditions", ())) != consumed_triples:
        raise RuntimeError("snapshot head preconditions mismatch")
    if (
        list(snapshot_payload.get("active_belief_refs", ())) != belief_refs
        or list(snapshot_payload.get("policy_revision_refs", ())) != policy_refs
    ):
        raise RuntimeError("snapshot typed revision refs mismatch")
    if (
        list(decision_payload.get("belief_revision_refs", ())) != belief_refs
        or list(decision_payload.get("policy_revision_refs", ())) != policy_refs
    ):
        raise RuntimeError("decision typed revision refs mismatch")

    candidates = decision_payload.get("candidates")
    if (
        not isinstance(candidates, Sequence)
        or isinstance(candidates, (str, bytes))
        or len(candidates) < 2
    ):
        raise RuntimeError("DecisionTrace candidates are incomplete")
    candidate_by_key: dict[str, Mapping[str, Any]] = {}
    for raw in candidates:
        if not isinstance(raw, Mapping):
            raise RuntimeError("DecisionTrace candidate is malformed")
        candidate = dict(raw)
        candidate_id = str(candidate.pop("candidate_id", ""))
        if candidate_id != "candidate-" + _digest(candidate)[:32]:
            raise RuntimeError("DecisionTrace candidate ID mismatch")
        key = _required(candidate.get("key"), "candidate.key")
        if key in candidate_by_key:
            raise RuntimeError("DecisionTrace candidate keys are not unique")
        candidate_by_key[key] = raw
    selection = decision_payload.get("selection")
    if not isinstance(selection, Mapping):
        raise RuntimeError("DecisionTrace selection is malformed")
    selected = candidate_by_key.get(str(selection.get("candidate_key") or ""))
    if selected is None or selection.get("candidate_id") != selected.get("candidate_id"):
        raise RuntimeError("DecisionTrace selection does not resolve")
    if selected.get("violated_value_keys"):
        raise RuntimeError("DecisionTrace selected candidate violates a hard constraint")
    rejected_keys = {
        str(item.get("candidate_key") or "")
        for item in decision_payload.get("rejected_reasons", ())
        if isinstance(item, Mapping)
    }
    if rejected_keys != set(candidate_by_key) - {str(selection["candidate_key"])}:
        raise RuntimeError("DecisionTrace rejection coverage mismatch")

    action_specs = decision_payload.get("action_specs")
    if not isinstance(action_specs, Sequence) or isinstance(action_specs, (str, bytes)):
        raise RuntimeError("DecisionTrace action specs are malformed")
    action_ids: list[str] = []
    effect_ids: list[str] = []
    for raw in action_specs:
        if not isinstance(raw, Mapping):
            raise RuntimeError("DecisionTrace action spec is malformed")
        action = dict(raw)
        action_id = str(action.get("action_id") or "")
        action_identity = {
            "decision_id": decision.object_id,
            "action_type": action.get("action_type"),
            "owner": action.get("owner"),
            "executor": action.get("executor"),
            "target_ref": action.get("target_ref"),
            "input_hash": action.get("input_hash"),
            "key": action.get("key"),
        }
        if "source_object" in action:
            action_identity["source_object"] = action["source_object"]
        expected_action_id = "material-action-" + _digest(action_identity)[:32]
        if action_id != expected_action_id:
            raise RuntimeError("DecisionTrace action ID mismatch")
        effect_id = str(action.get("effect_id") or "")
        if (
            effect_id
            != "material-effect-"
            + _digest(
                {
                    "action_id": action_id,
                    "expected_effect": action.get("expected_effect"),
                }
            )[:32]
        ):
            raise RuntimeError("DecisionTrace effect ID mismatch")
        if action.get("target_hash") != sha256_json(action.get("target_ref")):
            raise RuntimeError("DecisionTrace action target hash mismatch")
        action_ids.append(action_id)
        effect_ids.append(effect_id)
    state = str(decision_payload.get("decision_state") or "")
    if (
        (state == "approved" and not action_ids)
        or (state == "rejected" and action_ids)
        or state not in {"approved", "rejected"}
    ):
        raise RuntimeError("DecisionTrace decision/action state mismatch")
    if (
        list(decision_payload.get("action_refs", ())) != action_ids
        or list(decision_payload.get("effect_refs", ())) != effect_ids
    ):
        raise RuntimeError("DecisionTrace action/effect refs mismatch")
    if _contains_private_reasoning(decision_payload):
        raise RuntimeError("DecisionTrace contains prohibited private reasoning")
