"""Contracts and stable helpers for the cognitive graph store."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from core.cognitive.access_control import (
    cognitive_access_hash,
    derive_strictest_cognitive_access,
    make_cognitive_access_envelope,
    validate_cognitive_access_envelope,
)
from core.cognitive.material_effect_ledger import SqliteTargetEffectOracle
from core.cognitive.state_contract import sha256_json
from core.trust.formal_cognitive_mutation import formal_cognitive_mutation_input_hash

# Constants extracted from magic numbers
PENDING_LIMIT = 10000
COGNITIVE_GRAPH_READ_PURPOSE = "cognitive_graph_read"
COGNITIVE_GRAPH_DELETION_SCHEMA_VERSION = "mnemos.cognitive_graph_deletion.v1"
COGNITIVE_GRAPH_DELETION_TABLE = "cognitive_graph_deletion_receipts"
COGNITIVE_RELATION_ACTION = "upsert_relation"
COGNITIVE_RELATION_STALE_ACTION = "mark_relation_stale"
COGNITIVE_RELATION_DELETE_ACTION = "delete_relation"
COGNITIVE_CANONICAL_NODE_ACTION = "upsert_canonical_node"
COGNITIVE_RELATION_OWNER = "cognitive_graph"
COGNITIVE_RELATION_EXECUTOR = "cognitive_graph_store"
COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_ID = "project-contract:cognitive-graph-maintenance-rebuild"
COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_REVISION = (
    "mnemos.cognitive_graph_maintenance_material_effects.v1"
)
COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_TEXT = (
    "CognitiveGraph maintenance may rebuild only exact relations or canonical "
    "nodes derived from durable outbox, relation, or canonical-node snapshots."
)
COGNITIVE_GRAPH_MAINTENANCE_PRODUCER_HASH = sha256_json(
    {
        "module": "core.cognitive_graph.store",
        "producer": "CognitiveGraphStore.rebuild_missing_relations",
        "version": COGNITIVE_GRAPH_MAINTENANCE_CONTRACT_REVISION,
    }
)


class CognitiveGraphRelationEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed cognitive-graph relation upsert."""

    owner = COGNITIVE_RELATION_OWNER
    executor_id = COGNITIVE_RELATION_EXECUTOR
    action_type = COGNITIVE_RELATION_ACTION


class CognitiveGraphRelationStaleEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed cognitive-graph stale transition."""

    owner = COGNITIVE_RELATION_OWNER
    executor_id = COGNITIVE_RELATION_EXECUTOR
    action_type = COGNITIVE_RELATION_STALE_ACTION


class CognitiveGraphRelationDeleteEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed cognitive-graph relation deletion."""

    owner = COGNITIVE_RELATION_OWNER
    executor_id = COGNITIVE_RELATION_EXECUTOR
    action_type = COGNITIVE_RELATION_DELETE_ACTION


class CognitiveGraphCanonicalNodeEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed cognitive-graph canonical-node mutation."""

    owner = COGNITIVE_RELATION_OWNER
    executor_id = COGNITIVE_RELATION_EXECUTOR
    action_type = COGNITIVE_CANONICAL_NODE_ACTION


logger = logging.getLogger(__name__)


def _relation_id(source: str, target: str, relation_type: str) -> str:
    """确定性关系 ID"""
    key = f"{source}|{target}|{relation_type}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def cognitive_relation_material_action_binding(
    *,
    source: str,
    target: str,
    relation_type: str,
    strength: float = 0.5,
    confidence: float = 0.5,
    source_layer: str = "",
    target_layer: str = "",
    access_control: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Bind one relation upsert to its exact semantic and ACL payload."""

    rel_id = _relation_id(source, target, relation_type)
    effective_access = _strictest_graph_access(
        [access_control] if access_control is not None else [],
        object_ref=f"relation:{rel_id}",
    )
    metadata = {
        "access_control_hash": cognitive_access_hash(effective_access),
        "strength": strength,
        "confidence": confidence,
        "source_layer": source_layer,
        "target_layer": target_layer,
    }
    return {
        "target_ref": rel_id,
        "input_hash": formal_cognitive_mutation_input_hash(
            asset_kind="cognitive_graph_relation",
            action=COGNITIVE_RELATION_ACTION,
            target_ref=rel_id,
            actor="system",
            reason="cognitive_graph.add_relation",
            metadata=metadata,
        ),
    }


def cognitive_relation_stale_material_action_binding(
    relation_id: str,
) -> dict[str, str]:
    """Bind one relation stale transition to its stable relation identity."""

    normalized = str(relation_id or "").strip()
    if not normalized:
        raise ValueError("cognitive relation stale binding requires relation_id")
    metadata = {
        "relation_id": normalized,
        "desired_stale": True,
    }
    return {
        "target_ref": normalized,
        "input_hash": formal_cognitive_mutation_input_hash(
            asset_kind="cognitive_graph_relation",
            action=COGNITIVE_RELATION_STALE_ACTION,
            target_ref=normalized,
            actor="system",
            reason="cognitive_graph.mark_stale",
            metadata=metadata,
        ),
    }


def cognitive_relation_delete_material_action_binding(
    relation_id: str,
) -> dict[str, str]:
    """Bind one relation deletion to its stable relation identity."""

    normalized = str(relation_id or "").strip()
    if not normalized:
        raise ValueError("cognitive relation delete binding requires relation_id")
    metadata = {
        "relation_id": normalized,
        "desired_state": "absent",
    }
    return {
        "target_ref": normalized,
        "input_hash": formal_cognitive_mutation_input_hash(
            asset_kind="cognitive_graph_relation",
            action=COGNITIVE_RELATION_DELETE_ACTION,
            target_ref=normalized,
            actor="system",
            reason="cognitive_graph.delete_relation",
            metadata=metadata,
        ),
    }


def cognitive_canonical_node_material_action_binding(
    *,
    canonical_name: str,
    canonical_id: str,
    aliases: Sequence[str] | None = None,
    source_ids: Sequence[str] | None = None,
    embedding: bytes | None = None,
    access_control: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Bind one canonical-node mutation to its full content and ACL input."""

    normalized_id = str(canonical_id or "").strip()
    normalized_name = str(canonical_name or "").strip()
    if not normalized_id or not normalized_name:
        raise ValueError("cognitive canonical-node binding requires name and canonical_id")
    effective_access = _strictest_graph_access(
        [access_control] if access_control is not None else [],
        object_ref=f"canonical:{normalized_id}",
    )
    metadata = {
        "canonical_name": normalized_name,
        "aliases": sorted({str(value) for value in aliases or ()}),
        "source_ids": sorted({str(value) for value in source_ids or ()}),
        "embedding_hash": (
            "sha256:" + hashlib.sha256(embedding).hexdigest() if embedding is not None else ""
        ),
        "access_control_hash": cognitive_access_hash(effective_access),
    }
    return {
        "target_ref": normalized_id,
        "input_hash": formal_cognitive_mutation_input_hash(
            asset_kind="cognitive_graph_canonical_node",
            action=COGNITIVE_CANONICAL_NODE_ACTION,
            target_ref=normalized_id,
            actor="system",
            reason="cognitive_graph.add_canonical_node",
            metadata=metadata,
        ),
    }


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _decision_timestamp(value: str) -> str:
    """Normalize historical graph timestamps to an explicit UTC decision instant."""

    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _graph_deletion_scope_hash(scope_kind: str, scope_value: str) -> str:
    material = f"{str(scope_kind).strip().lower()}:{str(scope_value).strip()}"
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _graph_deletion_receipt_id(
    *,
    request_id: str,
    object_type: str,
    object_id: str,
    scope_hash: str,
) -> str:
    material = "|".join((request_id, object_type, object_id, scope_hash))
    return "coggraph-delete-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _wiki_urn(path: str) -> str:
    """将绝对/相对路径转换为 wiki:// URN"""
    if not path:
        return ""
    p = Path(path)
    if not p.is_absolute():
        return f"wiki://{p.as_posix().lstrip('./')}"
    parts = list(p.parts)
    if "mnemos" in parts:
        idx = parts.index("mnemos")
        rel = "/".join(parts[idx + 1 :])
        return f"wiki://{rel}"
    if "Documents" in parts and "raw" in parts:
        idx = parts.index("raw")
        rel = "/".join(parts[idx + 1 :])
        return f"wiki://raw/{rel}"
    return f"wiki://{p.name}"


def _session_urn(session_id: str) -> str:
    return f"session://{session_id}"


def _kg_urn(name: str) -> str:
    return f"kg://{name.strip()}"


def _obs_urn(obs_id: str) -> str:
    return f"obs://{obs_id}"


def _ref_urn(record_id: str) -> str:
    return f"ref://{record_id}"


def _feedback_urn(version: str = "latest") -> str:
    return f"feedback://persona/{version}"


def _layer_of_urn(urn: str) -> str:
    """从 URN 中提取层前缀，如 wiki://a.md -> wiki"""
    if not urn or "://" not in urn:
        return ""
    return urn.split("://", 1)[0]


def _restricted_graph_access(object_ref: str) -> Dict[str, Any]:
    """Represent an unproven graph object as unreadable, never public."""

    return make_cognitive_access_envelope(
        owner_principal_id="system:cognitive-graph",
        owner_agent="system",
        scope_type="cognitive_graph",
        scope_id=str(object_ref or "unknown"),
        purposes=(COGNITIVE_GRAPH_READ_PURPOSE,),
        consent_provenance_refs=(),
        sensitivity="restricted",
        retention_policy="cognitive_graph_retention",
        source_acl_lineage=(f"cognitive-graph:{object_ref or 'unknown'}",),
        visibility="restricted",
        scope_resolution="restricted_unknown",
        consent_status="restricted_unknown",
    )


def _parse_graph_access(raw_value: Any, object_ref: str) -> Dict[str, Any]:
    """Parse an ACL header without treating historical blank values as readable."""

    try:
        return validate_cognitive_access_envelope(json.loads(str(raw_value or "")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return _restricted_graph_access(object_ref)


def _strictest_graph_access(
    accesses: Sequence[Mapping[str, Any]],
    *,
    object_ref: str,
) -> Dict[str, Any]:
    """Derive graph ACLs from all source ACLs, failing closed on conflicts."""

    if not accesses:
        return _restricted_graph_access(object_ref)
    try:
        first = validate_cognitive_access_envelope(accesses[0])
        return derive_strictest_cognitive_access(
            accesses,
            owner_principal_id=str(first["owner"]["principal_id"]),
            owner_agent=str(first["owner"]["agent"]),
            scope_type=str(first["scope"]["scope_type"]),
            scope_id=str(first["scope"]["scope_id"]),
            purposes=(COGNITIVE_GRAPH_READ_PURPOSE,),
            retention_policy="cognitive_graph_retention",
        )
    except (KeyError, TypeError, ValueError):
        return _restricted_graph_access(object_ref)
