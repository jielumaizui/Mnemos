"""Private implementation module for successor_d0_generation.contract_inventory."""

from __future__ import annotations

from collections import defaultdict

from typing import Any

from typing import Mapping

from typing import Sequence

import json

from .model import (
    SUCCESSOR_CONSTITUTION_ANCHORS,
    SUCCESSOR_CONSTITUTION_REQUIREMENT_IDS,
    _TEST_PATH_PATTERN,
    _record,
    _reject_json_constant,
    _slug,
    _stable_digest,
    canonical_json_bytes,
    sha256_bytes,
)

from .snapshot import (
    _CatalogContext,
)


def _tuple_object_hook(value: dict[str, Any]) -> Any:
    if set(value) == {"__mnemos_tuple__"} and isinstance(value["__mnemos_tuple__"], list):
        return tuple(value["__mnemos_tuple__"])
    return value


def _canonical_requirement_specs(
    context: _CatalogContext,
) -> dict[str, dict[str, Any]]:
    relative = "scripts/phase1_governance_data.json"
    text = context.read_text(relative)
    if text is None:
        return {}
    try:
        payload = json.loads(
            text,
            object_hook=_tuple_object_hook,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        context.finding(
            "SCHEMA_INVALID",
            f"phase1 governance data is invalid: {exc}",
            source_ref=relative,
        )
        return {}
    if not isinstance(payload, Mapping):
        context.finding(
            "SCHEMA_INVALID", "phase1 governance data is not an object", source_ref=relative
        )
        return {}
    phase0 = payload.get("PHASE0_SUPPORT_REQUIREMENT_SPECS")
    phase1 = payload.get("PHASE1_ROOT_REQUIREMENT_SPECS")
    changed = payload.get("PHASE1_CHANGED_TEST_NODE_IDS_BY_ROOT")
    post_generation = payload.get("PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT")
    if not isinstance(phase0, (list, tuple)) or not isinstance(phase1, (list, tuple)):
        context.finding(
            "SCHEMA_INVALID", "governance requirement specs are unavailable", source_ref=relative
        )
        return {}
    changed = changed if isinstance(changed, Mapping) else {}
    post_generation = post_generation if isinstance(post_generation, Mapping) else {}
    specifications: dict[str, dict[str, Any]] = {}
    for raw in phase0:
        if not isinstance(raw, Mapping):
            continue
        spec = dict(raw)
        requirement_id = str(spec.get("requirement_id") or "")
        if requirement_id:
            specifications[requirement_id] = spec
    for raw in phase1:
        if not isinstance(raw, Mapping):
            continue
        spec = dict(raw)
        requirement_id = str(spec.get("requirement_id") or "")
        if not requirement_id:
            continue
        base_nodes = tuple(str(item) for item in spec.get("node_ids", ()))
        added_nodes = tuple(str(item) for item in changed.get(requirement_id, ()))
        spec["node_ids"] = (*base_nodes, *added_nodes)
        spec["post_generation_node_ids"] = tuple(
            str(item) for item in post_generation.get(requirement_id, ())
        )
        specifications[requirement_id] = spec
    return specifications


def _collect_requirements(
    context: _CatalogContext,
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, set[str]],
    dict[str, Any],
]:
    relative = "docs/acceptance/cognitive_requirement_test_manifest.json"
    payload = context.load_json(relative)
    stored_entries = payload.get("requirements") if isinstance(payload, Mapping) else None
    if not isinstance(stored_entries, list):
        return [], {}, {}, {}
    canonical_specs = _canonical_requirement_specs(context)
    stored_by_id: dict[str, dict[str, Any]] = {}
    for item in stored_entries:
        if not isinstance(item, Mapping) or not str(item.get("requirement_id") or ""):
            context.finding(
                "SCHEMA_INVALID", "requirement manifest entry is invalid", source_ref=relative
            )
            continue
        requirement_id = str(item["requirement_id"])
        if requirement_id in stored_by_id:
            context.finding(
                "DUPLICATE_ID",
                f"duplicate stored requirement ID: {requirement_id}",
                source_ref=relative,
            )
        stored_by_id[requirement_id] = dict(item)

    stored_node_refs = [
        str(node) for item in stored_by_id.values() for node in item.get("node_ids", [])
    ]
    canonical_node_refs = [
        str(node) for spec in canonical_specs.values() for node in spec.get("node_ids", ())
    ]
    stored_files = {node.split("::", 1)[0] for node in stored_node_refs}
    canonical_files = {node.split("::", 1)[0] for node in canonical_node_refs}
    mismatch_ids: set[str] = set()
    for requirement_id, spec in canonical_specs.items():
        stored = stored_by_id.get(requirement_id)
        if stored is None:
            mismatch_ids.add(requirement_id)
            continue
        if list(stored.get("node_ids", [])) != list(spec.get("node_ids", ())):
            mismatch_ids.add(requirement_id)
        if list(stored.get("post_generation_node_ids", [])) != list(
            spec.get("post_generation_node_ids", ())
        ):
            mismatch_ids.add(requirement_id)
    if mismatch_ids or set(canonical_specs) - set(stored_by_id):
        context.finding(
            "STALE_SOURCE",
            "stored requirement projection differs from canonical governance source data",
            source_ref=relative,
            evidence={
                "stored_sha256": context.sha256(relative),
                "stored_requirement_count": len(stored_by_id),
                "canonical_registered_requirement_count": len(canonical_specs),
                "stored_node_ref_count": len(stored_node_refs),
                "stored_unique_node_ref_count": len(set(stored_node_refs)),
                "stored_node_file_count": len(stored_files),
                "canonical_node_ref_count": len(canonical_node_refs),
                "canonical_unique_node_ref_count": len(set(canonical_node_refs)),
                "canonical_node_file_count": len(canonical_files),
                "mismatch_requirement_ids": sorted(mismatch_ids),
                "canonical_structure_sha256": sha256_bytes(
                    canonical_json_bytes(
                        [
                            {
                                "requirement_id": requirement_id,
                                "node_ids": list(spec.get("node_ids", ())),
                                "post_generation_node_ids": list(
                                    spec.get("post_generation_node_ids", ())
                                ),
                            }
                            for requirement_id, spec in sorted(canonical_specs.items())
                        ]
                    )
                ),
            },
            repair_action=(
                "regenerate cognitive_requirement_test_manifest.json from the exact canonical "
                "governance source before any D0 approval"
            ),
        )

    all_ids = sorted(set(stored_by_id) | set(canonical_specs))
    records: list[dict[str, Any]] = []
    node_requirements: dict[str, set[str]] = defaultdict(set)
    for requirement_id in all_ids:
        stored = stored_by_id.get(requirement_id, {})
        spec_record = canonical_specs.get(requirement_id)
        canonical_spec: Mapping[str, Any] = spec_record if spec_record is not None else {}
        canonical_nodes = (
            list(canonical_spec.get("node_ids", ()))
            if canonical_spec
            else list(stored.get("node_ids", []))
        )
        post_nodes = (
            list(canonical_spec.get("post_generation_node_ids", ()))
            if canonical_spec
            else list(stored.get("post_generation_node_ids", []))
        )
        for node in canonical_nodes:
            node_requirements[str(node)].add(requirement_id)
        semantic_contract = (
            canonical_spec.get("semantic_contract")
            if canonical_spec
            else (
                stored.get("fixture_contract", {}).get("semantic_contract")
                if isinstance(stored.get("fixture_contract"), Mapping)
                else None
            )
        )
        if requirement_id in mismatch_ids:
            status_value = "STALE_SOURCE"
        elif spec_record is not None:
            status_value = "CONTRACTED"
        else:
            status_value = "ADJUDICATION_REQUIRED"
        records.append(
            _record(
                "requirements",
                record_id=f"req:{_slug(requirement_id)}",
                discovery_key=f"requirement:{requirement_id}",
                record_status=status_value,
                evidence_refs=[
                    context.evidence(relative, anchor=requirement_id),
                    context.evidence("scripts/phase1_governance_data.json", anchor=requirement_id),
                ],
                requirement_id=requirement_id,
                legacy_requirement_id=requirement_id,
                title=requirement_id,
                kind=str(stored.get("requirement_kind") or "UNKNOWN"),
                normative_statement=semantic_contract,
                source_refs={
                    "root_id": stored.get("root_id"),
                    "finding_id": stored.get("finding_id"),
                    "work_package_id": stored.get("work_package_id")
                    or canonical_spec.get("work_package_id"),
                },
                scope=str(
                    stored.get("coverage_scope")
                    or canonical_spec.get("coverage_scope")
                    or "UNKNOWN"
                ),
                acceptance_observables={
                    "canonical_node_ids": canonical_nodes,
                    "post_generation_node_ids": post_nodes,
                    "stored_node_ids": list(stored.get("node_ids", [])),
                },
                risk_level=str(
                    stored.get("risk_level") or canonical_spec.get("risk_level") or "UNKNOWN"
                ),
                release_blocking=bool(stored.get("release_blocking", True)),
                contract_status=status_value,
                conflicts_with=[],
                supersedes=[],
                decision_ref=None,
            )
        )
    metrics = {
        "stored_requirement_count": len(stored_by_id),
        "canonical_registered_requirement_count": len(canonical_specs),
        "stored_node_ref_count": len(stored_node_refs),
        "stored_unique_node_ref_count": len(set(stored_node_refs)),
        "stored_node_file_count": len(stored_files),
        "canonical_node_ref_count": len(canonical_node_refs),
        "canonical_unique_node_ref_count": len(set(canonical_node_refs)),
        "canonical_node_file_count": len(canonical_files),
    }
    return records, canonical_specs, dict(node_requirements), metrics


def _successor_constitution_requirement_records() -> list[dict[str, Any]]:
    """Encode only the two successor principles explicitly accepted by the user.

    The design binding proves the exact source bytes, but v1 has no typed
    constitution approval receipt.  ``constitution_approval_missing`` therefore
    remains a mandatory blocker even when these two clauses are present.
    """

    clauses = (
        (
            SUCCESSOR_CONSTITUTION_REQUIREMENT_IDS[0],
            "Complete effective capability denominator before cutover",
            (
                "Development may proceed by capability cluster, but final cutover "
                "requires 100% of the approved effective capability denominator; "
                "no effective function may be deleted or permanently deferred."
            ),
            SUCCESSOR_CONSTITUTION_ANCHORS[0],
        ),
        (
            SUCCESSOR_CONSTITUTION_REQUIREMENT_IDS[1],
            "Keep legacy Mnemos frozen as oracle and rollback engine",
            (
                "Before cutover, legacy Mnemos remains frozen and available, with "
                "changes limited to highest-priority security or data-corruption "
                "repairs, and serves as data source, behavior reference, offline "
                "oracle, and rollback engine."
            ),
            SUCCESSOR_CONSTITUTION_ANCHORS[1],
        ),
    )
    records: list[dict[str, Any]] = []
    for requirement_id, title, statement, anchor in clauses:
        records.append(
            _record(
                "requirements",
                record_id=f"req:{_slug(requirement_id)}",
                discovery_key=f"requirement:{requirement_id}",
                record_status="CONTRACTED",
                evidence_refs=[],
                requirement_id=requirement_id,
                legacy_requirement_id=None,
                title=title,
                kind="SUCCESSOR_CONSTITUTION",
                normative_statement=statement,
                source_refs={
                    "binding_id": "successor_d0_design",
                    "anchor": anchor,
                    "approval_receipt": None,
                },
                scope="successor_cutover",
                acceptance_observables={
                    "exact_set_equality_required": True,
                    "permanent_deferral_allowed": False,
                },
                risk_level="CRITICAL",
                release_blocking=True,
                contract_status="CONTRACTED_PENDING_TYPED_APPROVAL",
                conflicts_with=[],
                supersedes=[],
                decision_ref=None,
            )
        )
    return records


def _collect_capabilities(
    context: _CatalogContext,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    relative = "docs/acceptance/function_matrix.json"
    payload = context.load_json(relative)
    features = payload.get("features") if isinstance(payload, Mapping) else None
    if not isinstance(features, list):
        return [], {}
    records: list[dict[str, Any]] = []
    feature_by_capability: dict[str, dict[str, Any]] = {}
    for feature in features:
        if not isinstance(feature, Mapping) or not str(feature.get("id") or ""):
            context.finding(
                "SCHEMA_INVALID", "function matrix feature is invalid", source_ref=relative
            )
            continue
        feature_id = str(feature["id"])
        capability_id = f"cap:{_slug(feature_id)}"
        if capability_id in feature_by_capability:
            context.finding(
                "DUPLICATE_ID",
                f"duplicate function-matrix capability ID: {capability_id}",
                source_ref=relative,
            )
        feature_by_capability[capability_id] = dict(feature)
        legacy_status = str(feature.get("status") or "UNKNOWN")
        records.append(
            _record(
                "capabilities",
                record_id=capability_id,
                discovery_key=f"function-matrix:{feature_id}",
                record_status="ADJUDICATION_REQUIRED",
                evidence_refs=[context.evidence(relative, anchor=feature_id)],
                capability_id=capability_id,
                legacy_feature_id=feature_id,
                title=str(feature.get("feature_name") or feature_id),
                domain=str(feature.get("group") or "UNKNOWN"),
                contract_revision=1,
                input_contract={"description": feature.get("expected_input")},
                output_contract={
                    "description": feature.get("expected_output"),
                    "success_standard": feature.get("success_standard"),
                },
                state_contract={
                    "canonical_owner_id": None,
                    "read_set": list(feature.get("data_dependencies", [])),
                    "write_set": [feature.get("data_landing")],
                    "atomicity": "UNKNOWN",
                },
                effect_contracts=[
                    {
                        "target_owner": None,
                        "idempotency": "UNKNOWN",
                        "observation": feature.get("expected_output"),
                        "compensation": "UNKNOWN",
                        "uncertain_rule": "UNKNOWN",
                    }
                ],
                failure_contract={"description": feature.get("failure_or_degradation")},
                data_contract={"dependencies": list(feature.get("data_dependencies", []))},
                security_privacy_contract="ADJUDICATION_REQUIRED",
                performance_contract_ref=None,
                migration_rollback_contract="ADJUDICATION_REQUIRED",
                legacy_behavior_state=legacy_status,
                surface_refs=[],
                requirement_refs=[],
                oracle_refs=[],
                contract_status="ADJUDICATION_REQUIRED",
                decision_ref=None,
            )
        )
    return records, feature_by_capability


def _test_file_from_ref(value: object) -> str:
    return str(value).split("::", 1)[0]


def _declared_test_paths(command: str) -> set[str]:
    return {_test_file_from_ref(match.group(0)) for match in _TEST_PATH_PATTERN.finditer(command)}


def _oracle_record(
    *,
    record_id: str,
    discovery_key: str,
    record_status: str,
    evidence_refs: Sequence[Mapping[str, Any]],
    kind: str,
    runner: Mapping[str, Any],
    source_anchors: Sequence[Mapping[str, Any]],
    fixture_refs: Sequence[Any] = (),
    mutation_operator_ids: Sequence[str] = (),
    fault_model_ids: Sequence[str] = (),
    asserted_observables: Sequence[Any] = (),
    evidence_schema: Any = None,
    population_policy: Any = "UNKNOWN",
    independence_class: str = "UNKNOWN",
    release_blocking: bool = True,
    **fields: Any,
) -> dict[str, Any]:
    return _record(
        "tests_oracles",
        record_id=record_id,
        discovery_key=discovery_key,
        record_status=record_status,
        evidence_refs=evidence_refs,
        kind=kind,
        runner=dict(runner),
        source_anchors=[dict(item) for item in source_anchors],
        fixture_refs=list(fixture_refs),
        mutation_operator_ids=list(mutation_operator_ids),
        fault_model_ids=list(fault_model_ids),
        asserted_observables=list(asserted_observables),
        evidence_schema=evidence_schema,
        population_policy=population_policy,
        independence_class=independence_class,
        release_blocking=release_blocking,
        decision_ref=None,
        **fields,
    )


def _collect_oracles(
    context: _CatalogContext,
    *,
    node_requirements: Mapping[str, set[str]],
    feature_by_capability: Mapping[str, Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    dict[str, list[str]],
    dict[str, str],
    dict[str, int],
]:
    records: list[dict[str, Any]] = []
    capability_oracles: dict[str, list[str]] = defaultdict(list)
    validation_oracle_by_key: dict[str, str] = {}
    declared_test_files: set[str] = set()

    # Canonical requirement nodes come from phase1_governance_data.json, not
    # from the stale stored requirement projection.
    for node_id, requirement_ids in sorted(node_requirements.items()):
        path = _test_file_from_ref(node_id)
        declared_test_files.add(path)
        exists = context.read_bytes(path, required=False) is not None
        record_id = f"oracle:pytest-node:{_stable_digest(node_id)}"
        if not exists:
            context.finding(
                "STALE_SOURCE",
                f"canonical requirement test node file is missing: {path}",
                artifact_id="tests_oracles",
                record_id=record_id,
                source_ref=path,
            )
        records.append(
            _oracle_record(
                record_id=record_id,
                discovery_key=f"pytest-node:{node_id}",
                record_status="REGISTERED_CANDIDATE" if exists else "STALE_SOURCE",
                evidence_refs=[
                    context.evidence("scripts/phase1_governance_data.json", anchor=node_id),
                    context.evidence(path, anchor=node_id.split("::", 1)[-1]),
                ],
                kind="requirement_pytest_node",
                runner={
                    "kind": "pytest",
                    "entrypoint": "python",
                    "argv": ["-m", "pytest", "-q", node_id],
                    "platform": "declared_by_requirement",
                    "lanes": [],
                    "exact_node_ids": [node_id],
                },
                source_anchors=[
                    {
                        "path": path,
                        "symbol": node_id.split("::", 1)[-1],
                        "sha256": context.sha256(path),
                    }
                ],
                asserted_observables=[],
                independence_class="UNKNOWN",
                requirement_refs=[f"req:{_slug(item)}" for item in sorted(requirement_ids)],
            )
        )

    behavior_path = "docs/acceptance/cognitive_behavior_scenarios.json"
    behavior = context.load_json(behavior_path)
    scenarios = behavior.get("scenarios") if isinstance(behavior, Mapping) else None
    if isinstance(scenarios, list):
        for scenario in scenarios:
            if not isinstance(scenario, Mapping) or not str(scenario.get("id") or ""):
                context.finding(
                    "SCHEMA_INVALID", "behavior scenario is invalid", source_ref=behavior_path
                )
                continue
            scenario_id = str(scenario["id"])
            tests = [_test_file_from_ref(item) for item in scenario.get("tests", [])]
            declared_test_files.update(tests)
            missing = sorted(
                path for path in tests if context.read_bytes(path, required=False) is None
            )
            if missing:
                context.finding(
                    "STALE_SOURCE",
                    f"behavior scenario has missing test paths: {scenario_id}",
                    source_ref=behavior_path,
                    evidence={"missing": missing},
                )
            records.append(
                _oracle_record(
                    record_id=f"oracle:behavior-scenario.{_slug(scenario_id)}",
                    discovery_key=f"behavior-scenario:{scenario_id}",
                    record_status="DECLARED_NOT_INDEPENDENT" if not missing else "STALE_SOURCE",
                    evidence_refs=[context.evidence(behavior_path, anchor=scenario_id)],
                    kind="behavior_scenario_bundle",
                    runner={
                        "kind": "pytest_file_bundle",
                        "entrypoint": "python",
                        "argv": ["-m", "pytest", "-q", *tests],
                        "platform": "UNKNOWN",
                        "lanes": [],
                        "exact_node_ids": [],
                    },
                    source_anchors=[
                        {"path": path, "symbol": None, "sha256": context.sha256(path)}
                        for path in tests
                    ],
                    asserted_observables=list(scenario.get("evidence_fields", [])),
                    independence_class="DECLARED_NOT_INDEPENDENT",
                    behavior_goal=scenario.get("behavior_goal"),
                    primary_tools=list(scenario.get("primary_tools", [])),
                )
            )

    ops_path = "docs/acceptance/ops_resilience_matrix.json"
    ops = context.load_json(ops_path)
    controls = ops.get("controls") if isinstance(ops, Mapping) else None
    if isinstance(controls, list):
        for control in controls:
            if not isinstance(control, Mapping) or not str(control.get("id") or ""):
                context.finding(
                    "SCHEMA_INVALID", "ops resilience control is invalid", source_ref=ops_path
                )
                continue
            control_id = str(control["id"])
            tests = [_test_file_from_ref(item) for item in control.get("tests", [])]
            declared_test_files.update(tests)
            missing = sorted(
                path for path in tests if context.read_bytes(path, required=False) is None
            )
            if missing:
                context.finding(
                    "STALE_SOURCE",
                    f"ops resilience control has missing test paths: {control_id}",
                    source_ref=ops_path,
                    evidence={"missing": missing},
                )
            records.append(
                _oracle_record(
                    record_id=f"oracle:ops-control.{_slug(control_id)}",
                    discovery_key=f"ops-resilience:{control_id}",
                    record_status="DECLARED_NOT_INDEPENDENT" if not missing else "STALE_SOURCE",
                    evidence_refs=[context.evidence(ops_path, anchor=control_id)],
                    kind="ops_resilience_control",
                    runner={
                        "kind": "declared_command_bundle",
                        "entrypoint": "shell",
                        "argv": list(control.get("validation_commands", [])),
                        "platform": "UNKNOWN",
                        "lanes": [],
                        "exact_node_ids": [],
                    },
                    source_anchors=[
                        {"path": path, "symbol": None, "sha256": context.sha256(path)}
                        for path in tests
                    ],
                    asserted_observables=[control.get("contract")],
                    independence_class="DECLARED_NOT_INDEPENDENT",
                    risk=control.get("risk"),
                    degradation_or_recovery=control.get("degradation_or_recovery"),
                )
            )

    function_path = "docs/acceptance/function_matrix.json"
    for capability_id, feature in sorted(feature_by_capability.items()):
        commands = feature.get("validation_commands", [])
        if not isinstance(commands, list):
            continue
        for ordinal, raw_command in enumerate(commands, start=1):
            command = str(raw_command)
            test_paths = sorted(_declared_test_paths(command))
            declared_test_files.update(test_paths)
            missing = sorted(
                path for path in test_paths if context.read_bytes(path, required=False) is None
            )
            oracle_id = f"oracle:function-validation.{_stable_digest([capability_id, command])}"
            if missing:
                context.finding(
                    "STALE_SOURCE",
                    f"function matrix validation command has a missing test path: {capability_id}",
                    artifact_id="tests_oracles",
                    record_id=oracle_id,
                    source_ref=function_path,
                    evidence={"command": command, "missing": missing},
                )
            records.append(
                _oracle_record(
                    record_id=oracle_id,
                    discovery_key=f"function-matrix:{capability_id}:validation:{ordinal}",
                    record_status="DECLARED_NOT_INDEPENDENT" if not missing else "STALE_SOURCE",
                    evidence_refs=[context.evidence(function_path, anchor=str(feature.get("id")))],
                    kind="function_validation_command",
                    runner={
                        "kind": (
                            "pytest"
                            if "pytest" in command
                            else ("script" if "scripts/" in command else "cli")
                        ),
                        "entrypoint": "shell",
                        "argv": [command],
                        "platform": "UNKNOWN",
                        "lanes": [],
                        "exact_node_ids": [],
                    },
                    source_anchors=[
                        {"path": path, "symbol": None, "sha256": context.sha256(path)}
                        for path in test_paths
                    ],
                    asserted_observables=[feature.get("success_standard")],
                    independence_class="DECLARED_NOT_INDEPENDENT",
                    capability_ref=capability_id,
                )
            )
            capability_oracles[capability_id].append(oracle_id)
            validation_oracle_by_key[f"{capability_id}:{ordinal}"] = oracle_id

    runtime_path = "docs/acceptance/cognitive_runtime_interface_manifest.json"
    runtime = context.load_json(runtime_path)
    interfaces = runtime.get("interfaces") if isinstance(runtime, Mapping) else None
    runtime_missing = 0
    if isinstance(interfaces, list):
        for interface in interfaces:
            if not isinstance(interface, Mapping) or not str(interface.get("interface_id") or ""):
                context.finding(
                    "SCHEMA_INVALID", "runtime interface entry is invalid", source_ref=runtime_path
                )
                continue
            interface_id = str(interface["interface_id"])
            runtime_missing += int(bool(interface.get("runtime_required", False)))
            source_anchors = []
            for item in [
                *interface.get("producer_anchors", []),
                *interface.get("consumer_anchors", []),
            ]:
                if not isinstance(item, Mapping):
                    continue
                anchor = dict(item)
                anchor_path = anchor.get("path")
                anchor["sha256"] = (
                    context.sha256(anchor_path)
                    if isinstance(anchor_path, str) and anchor_path
                    else None
                )
                source_anchors.append(anchor)
            records.append(
                _oracle_record(
                    record_id=f"oracle:runtime-interface.{_slug(interface_id)}",
                    discovery_key=f"runtime-interface:{interface_id}",
                    record_status="UNKNOWN",
                    evidence_refs=[context.evidence(runtime_path, anchor=interface_id)],
                    kind="runtime_target_effect_oracle",
                    runner={
                        "kind": "runtime_receipt",
                        "entrypoint": runtime.get("required_release_gate", {}).get("runner_path"),
                        "argv": [],
                        "platform": "production_or_qualified_target",
                        "lanes": [],
                        "exact_node_ids": [],
                    },
                    source_anchors=source_anchors,
                    asserted_observables=[interface.get("target_effect_oracle")],
                    evidence_schema=interface.get("evidence_mode"),
                    population_policy=interface.get("eligible_event_denominator"),
                    independence_class="REQUIRED_NOT_OBSERVED",
                    interface_id=interface_id,
                )
            )
    if runtime_missing:
        context.finding(
            "RUNTIME_EFFECT_EVIDENCE_MISSING",
            "runtime interface declarations have no independent target-effect evidence in D0",
            source_ref=runtime_path,
            evidence={
                "runtime_required_count": runtime_missing,
                "independent_effect_evidence_count": 0,
            },
            repair_action="collect detached independent runtime evidence; do not edit the D0 manifest",
        )

    audit_path = "docs/acceptance/audit_artifact_registry.json"
    audit = context.load_json(audit_path)
    audit_entries = audit.get("artifacts") if isinstance(audit, Mapping) else None
    unregistered_audits = 0
    if isinstance(audit_entries, list):
        for artifact in audit_entries:
            if not isinstance(artifact, Mapping) or not str(artifact.get("artifact_id") or ""):
                context.finding(
                    "SCHEMA_INVALID", "audit artifact entry is invalid", source_ref=audit_path
                )
                continue
            artifact_id = str(artifact["artifact_id"])
            status_value = str(artifact.get("validator_status") or "UNREGISTERED")
            unregistered_audits += int(status_value != "REGISTERED")
            runner_path = str(artifact.get("runner_path") or "")
            records.append(
                _oracle_record(
                    record_id=f"oracle:audit-artifact.{_slug(artifact_id)}",
                    discovery_key=f"audit-artifact:{artifact_id}",
                    record_status=status_value,
                    evidence_refs=[context.evidence(audit_path, anchor=artifact_id)],
                    kind="audit_artifact",
                    runner={
                        "kind": "script",
                        "entrypoint": runner_path,
                        "argv": list(artifact.get("execution_modes", [])),
                        "platform": "UNKNOWN",
                        "lanes": [],
                        "exact_node_ids": [],
                    },
                    source_anchors=[
                        {
                            "path": runner_path,
                            "symbol": artifact.get("validator_symbol"),
                            "sha256": context.sha256(runner_path),
                        }
                    ],
                    evidence_schema=artifact.get("artifact_schema"),
                    population_policy=artifact.get("required_population_policy"),
                    independence_class="UNKNOWN",
                    release_blocking=bool(artifact.get("release_blocking", True)),
                )
            )
    if unregistered_audits:
        context.finding(
            "UNREGISTERED_ORACLE",
            "audit artifact validators remain unregistered",
            source_ref=audit_path,
            evidence={"unregistered_count": unregistered_audits},
        )

    release_path = "docs/acceptance/cognitive_release_manifest.json"
    release = context.load_json(release_path)
    if isinstance(release, Mapping):
        gate_denominator = release.get("required_gate_denominator")
        if isinstance(gate_denominator, Mapping):
            for gate_id in gate_denominator.get("gate_ids", []):
                gate = str(gate_id)
                records.append(
                    _oracle_record(
                        record_id=f"oracle:release-gate.{_slug(gate)}",
                        discovery_key=f"release-gate:{gate}",
                        record_status="DENOMINATOR_DECLARED_NOT_EXECUTED",
                        evidence_refs=[context.evidence(release_path, anchor=gate)],
                        kind="release_gate",
                        runner={
                            "kind": "full_score_gate",
                            "entrypoint": gate_denominator.get("runner"),
                            "argv": [gate],
                            "platform": "strict-real-api-or-declared-profile",
                            "lanes": [],
                            "exact_node_ids": [],
                        },
                        source_anchors=[],
                        independence_class="UNKNOWN",
                    )
                )
        missing_certificates = 0
        for certificate in release.get("certificates", []):
            if not isinstance(certificate, Mapping):
                continue
            certificate_id = str(certificate.get("certificate_id") or "")
            if not certificate_id:
                continue
            status_value = str(certificate.get("status") or "MISSING")
            missing_certificates += int(status_value != "PRESENT")
            records.append(
                _oracle_record(
                    record_id=f"oracle:release-certificate.{_slug(certificate_id)}",
                    discovery_key=f"release-certificate:{certificate_id}",
                    record_status=status_value,
                    evidence_refs=[context.evidence(release_path, anchor=certificate_id)],
                    kind="release_certificate",
                    runner={
                        "kind": "certificate",
                        "entrypoint": None,
                        "argv": [],
                        "platform": "release",
                        "lanes": [],
                        "exact_node_ids": [],
                    },
                    source_anchors=[],
                    independence_class="REQUIRED_NOT_OBSERVED",
                )
            )
        if missing_certificates:
            context.finding(
                "RELEASE_CERTIFICATE_MISSING",
                "required release certificates are missing",
                source_ref=release_path,
                evidence={"missing_count": missing_certificates},
            )

    # Preserve the independent test-file denominator. UNLINKED is an inventory
    # state, not a coverage percentage and not automatically a functional gap.
    test_files = sorted(
        path.relative_to(context.root).as_posix()
        for path in (context.root / "tests").rglob("*.py")
        if path.is_file() and (path.name.startswith("test_") or path.name.endswith("_test.py"))
    )
    valid_declared = {path for path in declared_test_files if path in set(test_files)}
    for path in test_files:
        linked = path in valid_declared
        records.append(
            _oracle_record(
                record_id=f"oracle:test-file.{_stable_digest(path)}",
                discovery_key=f"test-file:{path}",
                record_status="LINKED_DECLARATION" if linked else "UNLINKED",
                evidence_refs=[context.evidence(path)],
                kind="pytest_file",
                source_path=path,
                runner={
                    "kind": "pytest_file",
                    "entrypoint": "python",
                    "argv": ["-m", "pytest", "-q", path],
                    "platform": "UNKNOWN",
                    "lanes": [],
                    "exact_node_ids": [],
                },
                source_anchors=[{"path": path, "symbol": None, "sha256": context.sha256(path)}],
                independence_class="UNCLASSIFIED_TEST_FILE",
                release_blocking=False,
                declaration_state="LINKED" if linked else "UNLINKED",
            )
        )
    metrics = {
        "test_file_denominator": len(test_files),
        "test_file_linked_declaration": len(valid_declared),
        "test_file_unlinked": len(set(test_files) - valid_declared),
        "declared_missing_test_file": len(declared_test_files - set(test_files)),
        "runtime_interface_required": runtime_missing,
        "audit_artifact_unregistered": unregistered_audits,
    }
    return records, dict(capability_oracles), validation_oracle_by_key, metrics
