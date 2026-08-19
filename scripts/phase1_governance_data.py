"""Typed loader for immutable Phase 1 governance specification data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.ops.durable_io import read_native_bytes

_DATA_PATH = Path(__file__).with_name("phase1_governance_data.json")
_TUPLE_TAG = "__mnemos_tuple__"
_EXPECTED_PHASE1_ROOT_ORDER = (
    "COG-045",
    "COG-001",
    "COG-002",
    "COG-004",
    "COG-005",
    "COG-006",
    "COG-007",
    "COG-008",
    "COG-003",
    "COG-009",
    "COG-026",
)
_EXPECTED_KEYS = frozenset(
    {
        "PHASE0_SUPPORT_REQUIREMENT_SPECS",
        "PHASE1_CLOSURE_BOUNDARIES",
        "PHASE1_CHANGED_TEST_NODE_IDS_BY_ROOT",
        "PHASE1_EXPLICIT_SOURCE_MUTATIONS",
        "PHASE1_MUTATION_ORACLE_NODES",
        "PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT",
        "PHASE1_REMOVED_TEST_SUPERSESSIONS",
        "PHASE1_REVALIDATION_BOUNDARY_OVERRIDES",
        "PHASE1_REVALIDATION_SEQUENCE",
        "PHASE1_ROOT_REQUIREMENT_SPECS",
    }
)


def _replacement_signatures(
    spec: dict[str, Any],
) -> tuple[tuple[str, str, str, str], ...]:
    replacements = list(spec.get("mutation_source_replacements", ()))
    singular = spec.get("mutation_source_replacement")
    if singular is not None and not isinstance(singular, dict):
        raise RuntimeError("phase1 governance singular source mutation is invalid")
    signatures: list[tuple[str, str, str, str]] = []
    for item in replacements:
        if not isinstance(item, dict):
            raise RuntimeError("phase1 governance source mutation is invalid")
        signatures.append(
            (
                str(item.get("operator_id") or ""),
                str(item.get("path") or ""),
                str(item.get("old") or ""),
                str(item.get("new") or ""),
            )
        )
    if isinstance(singular, dict):
        singular_signature = (
            str(singular.get("operator_id") or ""),
            str(singular.get("path") or ""),
            str(singular.get("old") or ""),
            str(singular.get("new") or ""),
        )
        # ``mutation_source_replacement`` is the compatibility singular view
        # of the plural denominator. Its exact alias is not a second mutation.
        if singular_signature not in signatures:
            signatures.append(singular_signature)
    return tuple(signatures)


def _valid_test_node_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("::")
    if len(parts) < 2 or not parts[0].startswith("tests/"):
        return False
    function_name = parts[-1].split("[", 1)[0]
    return function_name.startswith("test_")


def _validate_payload(value: dict[str, Any]) -> None:
    """Reject a self-inconsistent serialized governance denominator."""

    phase0_specs = value.get("PHASE0_SUPPORT_REQUIREMENT_SPECS")
    specs = value.get("PHASE1_ROOT_REQUIREMENT_SPECS")
    explicit = value.get("PHASE1_EXPLICIT_SOURCE_MUTATIONS")
    if (
        not isinstance(phase0_specs, tuple)
        or not phase0_specs
        or any(not isinstance(spec, dict) for spec in phase0_specs)
        or any(not str(spec.get("requirement_id") or "") for spec in phase0_specs)
        or len({str(spec.get("requirement_id") or "") for spec in phase0_specs})
        != len(phase0_specs)
    ):
        raise RuntimeError("phase0 support requirement denominator is invalid")
    if not isinstance(specs, tuple) or not isinstance(explicit, dict):
        raise RuntimeError("phase1 governance requirement denominator is invalid")

    requirement_ids: set[str] = set()
    spec_replacements: set[tuple[str, str, str, str, str]] = set()
    semantics_by_operator: dict[str, set[tuple[object, ...]]] = {}
    for spec in specs:
        if not isinstance(spec, dict):
            raise RuntimeError("phase1 governance requirement is invalid")
        tuple_fields = (
            "candidate_paths",
            "mutation_candidate_paths",
            "mutation_operator_ids",
            "fault_model_ids",
            "node_ids",
            "mutation_oracle_node_ids",
        )
        if any(not isinstance(spec.get(field), tuple) for field in tuple_fields):
            raise RuntimeError(
                "phase1 governance denominator sequence is noncanonical:"
                f"{spec.get('requirement_id') or ''}"
            )
        requirement_id = str(spec.get("requirement_id") or "")
        operator_ids = tuple(str(item) for item in spec.get("mutation_operator_ids", ()))
        fault_model_ids = tuple(str(item) for item in spec.get("fault_model_ids", ()))
        node_ids = tuple(str(item) for item in spec.get("node_ids", ()))
        oracle_ids = tuple(str(item) for item in spec.get("mutation_oracle_node_ids", ()))
        oracle_map = spec.get("mutation_oracle_node_ids_by_operator")
        if (
            not requirement_id
            or requirement_id in requirement_ids
            or not operator_ids
            or len(operator_ids) != len(set(operator_ids))
            or fault_model_ids != operator_ids
            or not node_ids
            or len(node_ids) != len(set(node_ids))
            or not oracle_ids
            or len(oracle_ids) != len(set(oracle_ids))
            or not set(oracle_ids) <= set(node_ids)
            or not isinstance(oracle_map, dict)
            or set(oracle_map) != set(operator_ids)
        ):
            raise RuntimeError(
                f"phase1 governance requirement contract is invalid:{requirement_id}"
            )
        requirement_ids.add(requirement_id)

        mapped_oracles: set[str] = set()
        for operator_id, mapped_nodes in oracle_map.items():
            if (
                not isinstance(mapped_nodes, tuple)
                or not mapped_nodes
                or any(not isinstance(node_id, str) or not node_id for node_id in mapped_nodes)
                or len(mapped_nodes) != len(set(mapped_nodes))
                or not set(mapped_nodes) <= set(node_ids)
            ):
                raise RuntimeError(
                    "phase1 governance mutation oracle map is invalid:"
                    f"{requirement_id}:{operator_id}"
                )
            mapped_oracles.update(mapped_nodes)
        if mapped_oracles != set(oracle_ids):
            raise RuntimeError(
                f"phase1 governance mutation oracle denominator is invalid:{requirement_id}"
            )

        replacements = _replacement_signatures(spec)
        if len(replacements) != len(set(replacements)):
            raise RuntimeError(
                "phase1 governance source mutation denominator contains duplicates:"
                f"{requirement_id}"
            )
        replacement_by_operator = {
            operator_id: (path, old, new) for operator_id, path, old, new in replacements
        }
        if len(replacement_by_operator) != len(replacements) or not set(
            replacement_by_operator
        ) <= set(operator_ids):
            raise RuntimeError(
                f"phase1 governance source mutation contract is invalid:{requirement_id}"
            )
        if set(replacement_by_operator) != set(operator_ids):
            raise RuntimeError(
                "phase1 governance source mutation operator denominator is incomplete:"
                f"{requirement_id}"
            )
        candidate_path_items = tuple(str(path) for path in spec.get("candidate_paths", ()))
        if (
            not candidate_path_items
            or any(not path for path in candidate_path_items)
            or len(candidate_path_items) != len(set(candidate_path_items))
        ):
            raise RuntimeError(
                "phase1 governance candidate path denominator is invalid:" f"{requirement_id}"
            )
        candidate_paths = set(candidate_path_items)
        for operator_id, (path, old, new) in replacement_by_operator.items():
            if path not in candidate_paths or not old or old == new:
                raise RuntimeError(
                    "phase1 governance source mutation target is invalid:"
                    f"{requirement_id}:{operator_id}"
                )
            spec_replacements.add((requirement_id, operator_id, path, old, new))
        mutation_path_items = tuple(str(path) for path in spec.get("mutation_candidate_paths", ()))
        if (
            not mutation_path_items
            or any(not path for path in mutation_path_items)
            or len(mutation_path_items) != len(set(mutation_path_items))
            or not set(mutation_path_items) <= candidate_paths
        ):
            raise RuntimeError(
                "phase1 governance mutation path denominator is invalid:" f"{requirement_id}"
            )
        for operator_id in operator_ids:
            replacement = replacement_by_operator.get(operator_id)
            if replacement is None:
                raise RuntimeError(
                    "phase1 governance source mutation operator denominator is incomplete:"
                    f"{requirement_id}:{operator_id}"
                )
            semantic: tuple[object, ...] = (
                "exact_source_replacement",
                *replacement,
            )
            semantics_by_operator.setdefault(operator_id, set()).add(semantic)

    explicit_replacement_items: list[tuple[str, str, str, str, str]] = []
    for requirement_id, value_item in explicit.items():
        items = value_item if isinstance(value_item, tuple) else (value_item,)
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError(f"phase1 explicit source mutation is invalid:{requirement_id}")
            explicit_replacement_items.append(
                (
                    str(requirement_id),
                    str(item.get("operator_id") or ""),
                    str(item.get("path") or ""),
                    str(item.get("old") or ""),
                    str(item.get("new") or ""),
                )
            )
    if len(explicit_replacement_items) != len(set(explicit_replacement_items)):
        raise RuntimeError("phase1 explicit source mutation denominator contains duplicates")
    explicit_replacements = set(explicit_replacement_items)
    expected_explicit_owners = {
        requirement_id for requirement_id, _operator_id, _path, _old, _new in spec_replacements
    }
    if set(explicit) != expected_explicit_owners:
        raise RuntimeError("phase1 explicit source mutation owner denominator is invalid")
    if explicit_replacements != spec_replacements:
        raise RuntimeError("phase1 explicit source mutation denominator is invalid")
    if any(len(semantics) != 1 for semantics in semantics_by_operator.values()):
        raise RuntimeError("phase1 mutation operator semantics are ambiguous")

    changed_test_nodes_by_root = value.get("PHASE1_CHANGED_TEST_NODE_IDS_BY_ROOT")
    root_requirement_ids = {
        requirement_id for requirement_id in requirement_ids if requirement_id.startswith("ROOT-")
    }
    if (
        not isinstance(changed_test_nodes_by_root, dict)
        or not set(changed_test_nodes_by_root) <= root_requirement_ids
    ):
        raise RuntimeError("phase1 changed test node owner denominator is invalid")
    existing_node_ids = {str(node_id) for spec in specs for node_id in spec.get("node_ids", ())}
    additional_node_ids: list[str] = []
    for root_id, node_ids_value in changed_test_nodes_by_root.items():
        if (
            not isinstance(node_ids_value, tuple)
            or not node_ids_value
            or any(not _valid_test_node_id(node_id) for node_id in node_ids_value)
            or len(node_ids_value) != len(set(node_ids_value))
        ):
            raise RuntimeError(f"phase1 changed test node owner is invalid:{root_id}")
        additional_node_ids.extend(node_ids_value)
    if len(additional_node_ids) != len(set(additional_node_ids)) or bool(
        set(additional_node_ids) & existing_node_ids
    ):
        raise RuntimeError("phase1 changed test node denominator is invalid")

    post_generation_nodes_by_root = value.get(
        "PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT"
    )
    if (
        not isinstance(post_generation_nodes_by_root, dict)
        or not set(post_generation_nodes_by_root) <= root_requirement_ids
    ):
        raise RuntimeError("phase1 post-generation test node owner denominator is invalid")
    post_generation_node_ids: list[str] = []
    for root_id, node_ids_value in post_generation_nodes_by_root.items():
        if (
            not isinstance(node_ids_value, tuple)
            or not node_ids_value
            or any(not _valid_test_node_id(node_id) for node_id in node_ids_value)
            or len(node_ids_value) != len(set(node_ids_value))
        ):
            raise RuntimeError(f"phase1 post-generation test node owner is invalid:{root_id}")
        post_generation_node_ids.extend(node_ids_value)
    if (
        len(post_generation_node_ids) != len(set(post_generation_node_ids))
        or bool(set(post_generation_node_ids) & existing_node_ids)
        or bool(set(post_generation_node_ids) & set(additional_node_ids))
    ):
        raise RuntimeError("phase1 post-generation test node denominator is invalid")

    removed_test_supersessions = value.get("PHASE1_REMOVED_TEST_SUPERSESSIONS")
    governed_node_ids = (
        existing_node_ids | set(additional_node_ids) | set(post_generation_node_ids)
    )
    if not isinstance(removed_test_supersessions, dict):
        raise RuntimeError("phase1 removed test supersession denominator is invalid")
    for removed_node_id, replacement_nodes in removed_test_supersessions.items():
        if (
            not isinstance(removed_node_id, str)
            or not _valid_test_node_id(removed_node_id)
            or not isinstance(replacement_nodes, tuple)
            or not replacement_nodes
            or any(
                not _valid_test_node_id(node_id) or node_id == removed_node_id
                for node_id in replacement_nodes
            )
            or len(replacement_nodes) != len(set(replacement_nodes))
            or not set(replacement_nodes) <= governed_node_ids
        ):
            raise RuntimeError("phase1 removed test supersession is invalid:" f"{removed_node_id}")

    configured_oracles = value.get("PHASE1_MUTATION_ORACLE_NODES")
    if not isinstance(configured_oracles, dict) or set(configured_oracles) != requirement_ids:
        raise RuntimeError("phase1 configured mutation oracle denominator is invalid")
    specs_by_id = {str(spec["requirement_id"]): spec for spec in specs}
    for requirement_id, configured in configured_oracles.items():
        spec = specs_by_id[str(requirement_id)]
        operator_ids = tuple(str(item) for item in spec["mutation_operator_ids"])
        if isinstance(configured, dict):
            normalized = {
                str(operator_id): (
                    (str(nodes),)
                    if isinstance(nodes, str)
                    else tuple(str(node_id) for node_id in nodes)
                )
                for operator_id, nodes in configured.items()
            }
        elif isinstance(configured, tuple):
            normalized = {
                operator_id: tuple(str(node_id) for node_id in configured)
                for operator_id in operator_ids
            }
        else:
            raise RuntimeError(f"phase1 configured mutation oracle is invalid:{requirement_id}")
        expected = {
            str(operator_id): tuple(str(node_id) for node_id in nodes)
            for operator_id, nodes in spec["mutation_oracle_node_ids_by_operator"].items()
        }
        if normalized != expected:
            raise RuntimeError(f"phase1 configured mutation oracle drift:{requirement_id}")

    ordered_root_requirement_ids = tuple(
        str(spec["requirement_id"])[len("ROOT-") :]
        for spec in specs
        if str(spec["requirement_id"]).startswith("ROOT-")
    )
    if ordered_root_requirement_ids != _EXPECTED_PHASE1_ROOT_ORDER:
        raise RuntimeError("phase1 root requirement order is invalid")
    boundaries = value.get("PHASE1_CLOSURE_BOUNDARIES")
    if not isinstance(boundaries, dict) or set(boundaries) != set(_EXPECTED_PHASE1_ROOT_ORDER):
        raise RuntimeError("phase1 closure boundary denominator is invalid")
    required_boundary_fields = {
        "code_contract_verified",
        "next_root_started",
        "production_effect",
        "production_mutation",
        "readiness_certified",
        "release_eligible",
        "root_closed",
    }

    def _boundary_is_valid(boundary: Any) -> bool:
        return (
            isinstance(boundary, dict)
            and required_boundary_fields <= set(boundary)
            and all(
                isinstance(boundary[field], bool)
                for field in (
                    "code_contract_verified",
                    "next_root_started",
                    "readiness_certified",
                    "release_eligible",
                    "root_closed",
                )
            )
            and all(
                isinstance(boundary[field], str) and bool(boundary[field])
                for field in ("production_effect", "production_mutation")
            )
        )

    if any(not _boundary_is_valid(boundary) for boundary in boundaries.values()):
        raise RuntimeError("phase1 closure boundary contract is invalid")

    sequence = value.get("PHASE1_REVALIDATION_SEQUENCE")
    if (
        not isinstance(sequence, tuple)
        or not sequence
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(part, str) and part for part in item)
            for item in sequence
        )
    ):
        raise RuntimeError("phase1 revalidation sequence is invalid")
    sequence_roots = tuple(item[0] for item in sequence)
    sequence_records = tuple(item[1] for item in sequence)
    if (
        len(sequence_records) != len(set(sequence_records))
        or not set(sequence_roots) <= set(_EXPECTED_PHASE1_ROOT_ORDER)
        or tuple(dict.fromkeys(sequence_roots)) != _EXPECTED_PHASE1_ROOT_ORDER
    ):
        raise RuntimeError("phase1 revalidation sequence denominator is invalid")

    overrides = value.get("PHASE1_REVALIDATION_BOUNDARY_OVERRIDES")
    if (
        not isinstance(overrides, dict)
        or not set(overrides) <= set(sequence_records)
        or any(not _boundary_is_valid(boundary) for boundary in overrides.values())
    ):
        raise RuntimeError("phase1 revalidation boundary override is invalid")


def _object_hook(value: dict[str, Any]) -> Any:
    if set(value) == {_TUPLE_TAG}:
        items = value[_TUPLE_TAG]
        if not isinstance(items, list):
            raise ValueError("phase1 governance tuple payload is invalid")
        return tuple(items)
    return value


def _load() -> dict[str, Any]:
    try:
        value = json.loads(
            read_native_bytes(_DATA_PATH).decode("utf-8"),
            object_hook=_object_hook,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise RuntimeError("phase1 governance data is unavailable") from None
    if not isinstance(value, dict) or set(value) != _EXPECTED_KEYS:
        raise RuntimeError("phase1 governance data contract is invalid")
    _validate_payload(value)
    return value


_DATA = _load()
PHASE0_SUPPORT_REQUIREMENT_SPECS = _DATA["PHASE0_SUPPORT_REQUIREMENT_SPECS"]
PHASE1_CLOSURE_BOUNDARIES = _DATA["PHASE1_CLOSURE_BOUNDARIES"]
PHASE1_CHANGED_TEST_NODE_IDS_BY_ROOT = _DATA["PHASE1_CHANGED_TEST_NODE_IDS_BY_ROOT"]
PHASE1_EXPLICIT_SOURCE_MUTATIONS = _DATA["PHASE1_EXPLICIT_SOURCE_MUTATIONS"]
PHASE1_MUTATION_ORACLE_NODES = _DATA["PHASE1_MUTATION_ORACLE_NODES"]
PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT = _DATA[
    "PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT"
]
PHASE1_REMOVED_TEST_SUPERSESSIONS = _DATA["PHASE1_REMOVED_TEST_SUPERSESSIONS"]
PHASE1_REVALIDATION_BOUNDARY_OVERRIDES = _DATA["PHASE1_REVALIDATION_BOUNDARY_OVERRIDES"]
PHASE1_REVALIDATION_SEQUENCE = _DATA["PHASE1_REVALIDATION_SEQUENCE"]


def _with_changed_test_nodes(
    specs: tuple[dict[str, Any], ...],
    node_ids_by_root: dict[str, tuple[str, ...]],
    post_generation_node_ids_by_root: dict[str, tuple[str, ...]],
) -> tuple[dict[str, Any], ...]:
    expanded: list[dict[str, Any]] = []
    for spec in specs:
        root_id = str(spec["requirement_id"])
        additional = node_ids_by_root.get(root_id, ())
        expanded.append(
            {
                **spec,
                "node_ids": (*spec["node_ids"], *additional),
                "post_generation_node_ids": post_generation_node_ids_by_root.get(
                    root_id,
                    (),
                ),
            }
        )
    return tuple(expanded)


PHASE1_ROOT_REQUIREMENT_SPECS = _with_changed_test_nodes(
    _DATA["PHASE1_ROOT_REQUIREMENT_SPECS"],
    PHASE1_CHANGED_TEST_NODE_IDS_BY_ROOT,
    PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT,
)
