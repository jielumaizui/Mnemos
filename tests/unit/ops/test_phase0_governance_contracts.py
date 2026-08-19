from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path

import pytest

from scripts import check_maintainability_budget as maintainability
from scripts import check_zombie_code_policy as zombie_policy
from scripts import generate_phase0_governance_contracts as governance
from scripts import phase1_governance_data as phase1_data
from scripts import refresh_phase1_deep_audit_governance as phase1_refresh

requires_desktop_governance = pytest.mark.skipif(
    not (
        governance.GOVERNING_CONTRACT_PATH.is_file() and governance.HISTORICAL_SOURCE_PATH.is_file()
    ),
    reason="local governing Desktop assets are unavailable",
)


def _materialize(tmp_path: Path, monkeypatch) -> Path:
    acceptance = tmp_path / "acceptance"
    acceptance.mkdir()
    source_manifest = (
        governance.ROOT / "docs" / "acceptance" / "cognitive_runtime_interface_manifest.json"
    )
    (acceptance / source_manifest.name).write_bytes(source_manifest.read_bytes())
    document_manifest = governance.ROOT / "docs" / "acceptance" / "document_asset_manifest.json"
    (acceptance / document_manifest.name).write_bytes(document_manifest.read_bytes())
    monkeypatch.setattr(governance, "ACCEPTANCE", acceptance)
    for path, content in governance.build_assets().items():
        path.write_text(content, encoding="utf-8")
    return acceptance


def test_phase0_governance_assets_are_current() -> None:
    assert governance.validate_assets(desktop_mode="skip") == []


@requires_desktop_governance
def test_local_desktop_governance_assets_are_current() -> None:
    assert governance.validate_assets(desktop_mode="required") == []


def test_repo_only_validation_is_portable_without_desktop_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        governance,
        "GOVERNING_CONTRACT_PATH",
        tmp_path / governance.GOVERNING_CONTRACT_PATH.name,
    )
    monkeypatch.setattr(
        governance,
        "GOVERNING_CONTRACT_PREDECESSOR_PATH",
        tmp_path / governance.GOVERNING_CONTRACT_PREDECESSOR_PATH.name,
    )
    monkeypatch.setattr(
        governance,
        "HISTORICAL_SOURCE_PATH",
        tmp_path / governance.HISTORICAL_SOURCE_PATH.name,
    )

    errors = governance.validate_assets(desktop_mode="skip")

    assert not any("governing contract" in error for error in errors)
    assert not any("historical source" in error for error in errors)
    assert "renamed predecessor provenance mismatch" not in errors


def test_ci_runs_repo_only_phase0_governance_validation() -> None:
    workflow = (governance.ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert (
        "python scripts/generate_phase0_governance_contracts.py " "--desktop-mode skip --json"
    ) in workflow
    assert "PYTHONPATH=." not in workflow


def test_root_finding_and_interface_denominators_are_exact() -> None:
    assert len(governance.ROOT_ORDER) == 50
    assert len({root_id for root_id, _ in governance.ROOT_ORDER}) == 50
    assert len(governance.FINDING_OWNERS) == 38
    manifest = json.loads(
        (governance.ACCEPTANCE / "cognitive_runtime_interface_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert len({item["interface_id"] for item in manifest["interfaces"]}) == 13


def test_generated_root_projection_is_bidirectional() -> None:
    dag = json.loads(
        (governance.ACCEPTANCE / "cognitive_root_dag.json").read_text(encoding="utf-8")
    )
    direct = {item["root_id"]: set(item["direct_finding_ids"]) for item in dag["roots"]}
    for finding_id, owners in governance.FINDING_OWNERS.items():
        for root_id in owners:
            assert finding_id in direct[root_id]
    for root_id, finding_ids in direct.items():
        for finding_id in finding_ids:
            assert root_id in governance.FINDING_OWNERS[finding_id]


def test_root_or_interface_denominator_deletion_is_blocking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    acceptance = _materialize(tmp_path, monkeypatch)
    dag_path = acceptance / "cognitive_root_dag.json"
    dag = json.loads(dag_path.read_text(encoding="utf-8"))
    dag["roots"].pop()
    dag_path.write_text(json.dumps(dag), encoding="utf-8")

    errors = governance.validate_assets()

    assert "stale generated asset: cognitive_root_dag.json" in errors

    for path, content in governance.build_assets().items():
        path.write_text(content, encoding="utf-8")
    manifest_path = acceptance / "cognitive_runtime_interface_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["interfaces"].pop()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert "runtime interface manifest denominator must be exactly 13" in (
        governance.validate_assets()
    )


def test_support_work_packages_have_canonical_parent_roots() -> None:
    root_ids = {root_id for root_id, _ in governance.ROOT_ORDER}
    assert set(governance.SUPPORT_WPS.values()) <= root_ids
    assert governance.SUPPORT_WPS == {
        "WP-COG-025-SAFETY": "COG-025",
        "WP-COG-040-P0-BASELINE": "COG-040",
        "WP-COG-046-P0-DENOMINATOR-LOCK": "COG-046",
    }


def test_phase0_support_requirements_are_exact_and_fully_registered() -> None:
    manifest = json.loads(
        (governance.ACCEPTANCE / "cognitive_requirement_test_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    requirements = [
        item
        for item in manifest["requirements"]
        if item.get("coverage_scope") == "phase0_support_wp"
    ]

    assert len(requirements) == 14
    assert manifest["phase0_support_requirement_count"] == 14
    assert manifest["phase0_support_registered_count"] == 14
    assert manifest["phase0_exact_node_count"] == 41
    assert manifest["phase0_support_coverage_percent"] == 100
    assert set(manifest["phase0_support_work_packages"]) == set(governance.SUPPORT_WPS)
    assert all(item["status"] == "REGISTERED" for item in requirements)
    assert all(item["work_package_id"] in governance.SUPPORT_WPS for item in requirements)
    assert all(item["runner_kind"] == "pytest" for item in requirements)
    assert all(item["entrypoint"] == "python" for item in requirements)
    assert all(item["argv"] == ["-m", "pytest", "-q"] for item in requirements)
    assert all(
        item["required_population_policy"].startswith("all_exact_nodes_required_")
        for item in requirements
    )
    assert all(item["node_ids"] for item in requirements)
    assert sum(len(item["node_ids"]) for item in requirements) == 41
    assert all(len(item["node_ids"]) == len(set(item["node_ids"])) for item in requirements)
    assert all("::" in node_id for item in requirements for node_id in item["node_ids"])
    assert all(item["mutation_operator_ids"] for item in requirements)
    assert all(item["baseline_expected_failure"] for item in requirements)
    assert all(item["baseline_artifact_ref"] for item in requirements)
    assert all(item["candidate_artifact_ref"] for item in requirements)
    assert all(
        governance._pytest_node_exists(node_id) and governance._pytest_node_has_assertion(node_id)
        for item in requirements
        for node_id in item["node_ids"]
    )
    assert all(item["candidate_artifacts"] for item in requirements)
    assert all(
        candidate["sha256"]
        == governance.hashlib.sha256((governance.ROOT / candidate["path"]).read_bytes()).hexdigest()
        for item in requirements
        for candidate in item["candidate_artifacts"]
    )
    assert all(
        item["candidate_artifact_ref"] == "sha256:" + governance._hash(item["candidate_artifacts"])
        for item in requirements
    )
    darwin_only = [item for item in requirements if item["execution_platforms"] == ["darwin"]]
    assert {item["requirement_id"] for item in darwin_only} == {
        "ROOT-COG-046",
        "FINDING-GLOB-001-COG-025",
    }
    assert all(
        item["execution_platforms"] == ["all"]
        for item in requirements
        if item["requirement_id"] not in {"ROOT-COG-046", "FINDING-GLOB-001-COG-025"}
    )
    for item in requirements:
        markers = set().union(
            *(governance._pytest_node_outcome_markers(node_id) for node_id in item["node_ids"])
        )
        assert "xfail" not in markers
        if item["execution_platforms"] == ["all"]:
            assert not ({"skip", "skipif"} & markers)
        else:
            assert {"skip", "skipif"} & markers


def test_phase1_darwin_requirement_accepts_capability_markers_but_not_xfail(
    monkeypatch,
) -> None:
    spec = next(
        item
        for item in governance.PHASE1_ROOT_REQUIREMENT_SPECS
        if item["requirement_id"] == "ROOT-COG-045"
    )

    assert spec["execution_platforms"] == ("darwin",)
    assert governance._required_population_policy(spec) == (  # noqa: SLF001
        "all_exact_nodes_required_no_skip_no_xfail_on_darwin"
    )
    assert governance._phase1_static_outcome_markers_are_valid(  # noqa: SLF001
        spec,
        spec["node_ids"],
    )
    assert not governance._phase1_static_outcome_markers_are_valid(  # noqa: SLF001
        {**spec, "execution_platforms": ("all",)},
        spec["node_ids"],
    )

    monkeypatch.setattr(
        governance,
        "_pytest_node_outcome_markers",
        lambda _node_id: {"xfail"},
    )
    assert not governance._phase1_static_outcome_markers_are_valid(  # noqa: SLF001
        spec,
        spec["node_ids"],
    )


def test_split_governance_and_runtime_modules_remain_in_candidate_denominators() -> None:
    """A façade split must not silently remove executable code from evidence hashes."""

    all_specs = (
        *governance.PHASE0_SUPPORT_REQUIREMENT_SPECS,
        *governance.PHASE1_ROOT_REQUIREMENT_SPECS,
    )
    governance_dependencies = {
        "scripts/phase0_governance_constants.py",
        "scripts/phase0_governance_inventory.py",
        "scripts/phase1_governance_data.py",
        "scripts/phase1_governance_data.json",
        "scripts/phase1_governance_execution_validation.py",
        "scripts/phase1_governance_ledger_validation.py",
    }
    for spec in all_specs:
        candidates = set(spec.get("candidate_paths", ()))
        if "scripts/generate_phase0_governance_contracts.py" in candidates:
            assert governance_dependencies <= candidates

    phase1_specs = {
        str(spec["requirement_id"]): set(spec.get("candidate_paths", ()))
        for spec in governance.PHASE1_ROOT_REQUIREMENT_SPECS
    }
    assert {
        "core/runtime_environment.py",
        "core/sync_framework/native_artifact_bounded_parse.py",
        "core/sync_framework/native_artifact_models.py",
        "core/sync_framework/native_file_io.py",
        "core/sync_framework/raw_current_projection_store.py",
        "scripts/agent_source_raw_reconciliation_cli.py",
        "scripts/agent_source_raw_reconciliation_support.py",
        "scripts/agent_source_raw_worker_runtime.py",
        "scripts/agent_source_raw_worker_sandbox.py",
        "scripts/agent_source_support_runtime_audit.py",
        "core/mnemos_bus.py",
        "core/trust/static_sink_registry.json",
    } <= phase1_specs["ROOT-COG-045"]
    assert {
        "core/hephaestus/distill_entrypoint_audit.py",
        "core/kia/amphora_cli.py",
        "core/trust/static_sink_registry.json",
    } <= phase1_specs["ROOT-COG-008"]
    assert "daemon/raw_projection_state.py" in phase1_specs["ROOT-COG-026"]
    assert (
        "daemon/raw_projection_state.py"
        in phase1_specs["FINDING-P12-003-COG-026"]
    )

    for requirement_id in (
        "ROOT-COG-004",
        "ROOT-COG-005",
        "ROOT-COG-006",
        "ROOT-COG-007",
        "ROOT-COG-003",
    ):
        assert (
            "scripts/agent_source_support_runtime_audit.py"
            in phase1_specs[requirement_id]
        )


def test_phase1_refresh_derives_changed_executable_candidate_coverage(
    monkeypatch,
) -> None:
    """The refresh claim must be calculated from Git paths, not self-declared."""

    def fake_git_paths(*args: str) -> set[str]:
        if args[:2] == ("diff", "--name-only"):
            return {"core/covered.py", "docs/not-executable.md"}
        assert args == ("ls-files", "--others", "--exclude-standard")
        return {
            "scripts/missing.py",
            "tests/unit/test_not_executable_denominator.py",
        }

    monkeypatch.setattr(phase1_refresh, "_git_paths", fake_git_paths)
    monkeypatch.setattr(
        governance,
        "PHASE1_ROOT_REQUIREMENT_SPECS",
        ({"candidate_paths": ("core/covered.py",)},),
    )

    coverage = phase1_refresh._phase1_changed_executable_path_coverage()  # noqa: SLF001

    assert coverage["changed_executable_paths"] == [
        "core/covered.py",
        "scripts/missing.py",
    ]
    assert coverage["missing_candidate_paths"] == ["scripts/missing.py"]
    assert coverage["changed_executable_path_count"] == 2
    assert len(coverage["changed_executable_paths_sha256"]) == 64
    assert len(coverage["candidate_paths_sha256"]) == 64


def test_phase1_refresh_git_path_nul_flag_precedes_pathspec_boundary(
    monkeypatch,
) -> None:
    """Tracked changes must not disappear because ``-z`` became a pathspec."""

    observed: list[list[str]] = []

    def fake_check_output(argv, *, cwd):
        observed.append(list(argv))
        assert cwd == phase1_refresh.ROOT
        return b"core/changed.py\0"

    monkeypatch.setattr(phase1_refresh.subprocess, "check_output", fake_check_output)

    assert phase1_refresh._git_paths(  # noqa: SLF001
        "diff",
        "--name-only",
        "candidate-commit",
        "--",
    ) == {"core/changed.py"}
    assert observed == [
        [
            "git",
            "diff",
            "--name-only",
            "candidate-commit",
            "-z",
            "--",
        ]
    ]


def test_phase1_refresh_binds_every_changed_test_to_an_oracle_denominator(
    monkeypatch,
) -> None:
    """A new red test cannot remain invisible to every governed Root."""

    def fake_git_paths(*args: str) -> set[str]:
        if args[:2] == ("diff", "--name-only"):
            return {
                "tests/unit/test_covered.py",
                "tests/unit/test_missing.py",
                "core/implementation.py",
            }
        assert args == ("ls-files", "--others", "--exclude-standard")
        return {"tests/integration/test_untracked.py"}

    monkeypatch.setattr(phase1_refresh, "_git_paths", fake_git_paths)
    monkeypatch.setattr(
        governance,
        "PHASE1_ROOT_REQUIREMENT_SPECS",
        (
            {
                "node_ids": (
                    "tests/unit/test_covered.py::test_contract",
                    "tests/integration/test_untracked.py::test_contract",
                )
            },
        ),
    )
    monkeypatch.setattr(
        governance,
        "PHASE1_CHANGED_TEST_NODE_IDS_BY_ROOT",
        {},
    )
    monkeypatch.setattr(
        governance,
        "PHASE1_REMOVED_TEST_SUPERSESSIONS",
        {},
    )
    monkeypatch.setattr(
        phase1_refresh,
        "_phase1_changed_test_function_delta",
        lambda _paths: (
            (
                "tests/integration/test_untracked.py::test_contract",
                "tests/unit/test_covered.py::test_contract",
                "tests/unit/test_covered.py::test_unregistered_same_file",
                "tests/unit/test_missing.py::test_contract",
            ),
            (),
        ),
    )

    coverage = phase1_refresh._phase1_changed_test_oracle_coverage()  # noqa: SLF001

    assert coverage["changed_test_paths"] == [
        "tests/integration/test_untracked.py",
        "tests/unit/test_covered.py",
        "tests/unit/test_missing.py",
    ]
    assert coverage["missing_oracle_test_paths"] == [
        "tests/unit/test_missing.py"
    ]
    assert coverage["missing_oracle_test_node_ids"] == [
        "tests/unit/test_covered.py::test_unregistered_same_file",
        "tests/unit/test_missing.py::test_contract",
    ]
    assert coverage["changed_or_added_test_node_count"] == 4
    assert coverage["changed_test_path_count"] == 3
    assert len(coverage["changed_test_paths_sha256"]) == 64
    assert len(coverage["oracle_test_paths_sha256"]) == 64


def test_phase1_changed_test_ast_contract_detects_same_file_function_change() -> None:
    before = """
class TestContract:
    def test_registered(self):
        assert True
"""
    after = """
class TestContract:
    def test_registered(self):
        assert False

    def test_new_case(self):
        assert True
"""

    before_contracts = phase1_refresh._test_function_contracts(  # noqa: SLF001
        before,
        "tests/unit/test_contract.py",
    )
    after_contracts = phase1_refresh._test_function_contracts(  # noqa: SLF001
        after,
        "tests/unit/test_contract.py",
    )

    registered = "tests/unit/test_contract.py::TestContract::test_registered"
    added = "tests/unit/test_contract.py::TestContract::test_new_case"
    assert before_contracts[registered] != after_contracts[registered]
    assert added not in before_contracts
    assert added in after_contracts


def test_phase1_refresh_derives_static_debt_metrics(
    monkeypatch,
) -> None:
    low = {
        "filename": "scripts/refresh_phase1_deep_audit_governance.py",
        "test_id": "B603",
        "issue_severity": "LOW",
        "issue_confidence": "HIGH",
        "issue_text": "fixed argv subprocess",
    }
    medium = {
        "filename": "scripts/refresh_phase1_deep_audit_governance.py",
        "test_id": "B999",
        "issue_severity": "MEDIUM",
        "issue_confidence": "HIGH",
        "issue_text": "blocking synthetic finding",
    }
    executions = iter(([low, low, medium], [low]))
    monkeypatch.setattr(
        phase1_refresh,
        "_run_bandit_json",
        lambda _paths, *, cwd: next(executions),
    )
    monkeypatch.setattr(
        phase1_refresh,
        "_git_blob_text",
        lambda _commit, _path: "pass\n",
    )

    report = phase1_refresh._phase1_bandit_delta(  # noqa: SLF001
        ["scripts/refresh_phase1_deep_audit_governance.py"]
    )

    assert report["new_or_increased_finding_count"] == 2
    assert report["severity_counts"] == {"LOW": 1, "MEDIUM": 1}
    assert report["warning_count"] == 1
    assert report["blocking_count"] == 1
    assert report["release_blocking"] is True

    import_report = phase1_refresh._phase1_import_time_cycle_contract()  # noqa: SLF001
    assert import_report["current_import_time_cycle_count"] == len(
        import_report["current_import_time_cycles"]
    )
    assert import_report["blocking_count"] == import_report[
        "current_import_time_cycle_count"
    ]

    source = inspect.getsource(phase1_refresh)
    assert '"new_phase1_bandit_finding_count": 0' not in source
    assert '"new_phase1_import_cycle_count": 0' not in source
    assert '"new_phase1_zombie_candidate_count": 0' not in source


def test_phase1_changed_paths_add_no_unplanned_engineering_debt() -> None:
    """Changed Phase 1 code has no hidden budget debt or unnamed zombie lane."""

    coverage = phase1_refresh._phase1_changed_executable_path_coverage()  # noqa: SLF001
    changed_paths = set(coverage["changed_executable_paths"])

    metrics = maintainability.scan_repo(governance.ROOT)
    budget = maintainability.load_budget(maintainability.BUDGET_FILE)
    _ok, report = maintainability.check_budget(
        metrics,
        budget,
        closure=True,
    )
    changed_failures = [
        failure
        for failure in report["failures"]
        if str(failure.get("path") or "") in changed_paths
    ]
    assert changed_failures == []

    python_paths = [
        governance.ROOT / path
        for path in sorted(changed_paths)
        if path.endswith(".py") and (governance.ROOT / path).is_file()
    ]
    findings = zombie_policy.scan_project(
        paths=python_paths,
        project_root=governance.ROOT,
    )
    current_pending = {
        (finding.path, finding.qualified_name, finding.kind)
        for finding in findings
    }
    assert current_pending == set()


def test_phase1_mutation_replacements_target_exact_current_source() -> None:
    """Every governed mutation must still target one exact current source span."""

    spec_replacements: set[tuple[str, str, str, str, str]] = set()
    for spec in governance.PHASE1_ROOT_REQUIREMENT_SPECS:
        requirement_id = str(spec["requirement_id"])
        replacements = list(spec.get("mutation_source_replacements", ()))
        singular = spec.get("mutation_source_replacement")
        if singular is not None:
            replacements.append(singular)
        candidate_paths = set(spec.get("candidate_paths", ()))
        for replacement in replacements:
            path = str(replacement["path"])
            old = str(replacement["old"])
            new = str(replacement["new"])
            signature = (
                requirement_id,
                str(replacement["operator_id"]),
                path,
                old,
                new,
            )
            if signature in spec_replacements:
                continue
            spec_replacements.add(signature)
            assert path in candidate_paths
            assert old != new
            assert (governance.ROOT / path).read_text(encoding="utf-8").count(old) == 1

    explicit_replacements: set[tuple[str, str, str, str, str]] = set()
    for requirement_id, value in governance.PHASE1_EXPLICIT_SOURCE_MUTATIONS.items():
        replacements = value if isinstance(value, tuple) else (value,)
        for replacement in replacements:
            explicit_replacements.add(
                (
                    str(requirement_id),
                    str(replacement["operator_id"]),
                    str(replacement["path"]),
                    str(replacement["old"]),
                    str(replacement["new"]),
                )
            )

    assert explicit_replacements == spec_replacements


def test_phase1_mutation_operator_ids_have_one_global_fault_semantic() -> None:
    """A reused operator id must describe the same executable fault everywhere."""

    semantics_by_operator: dict[str, set[tuple[object, ...]]] = {}
    owners_by_operator: dict[str, set[str]] = {}
    for spec in governance.PHASE1_ROOT_REQUIREMENT_SPECS:
        requirement_id = str(spec["requirement_id"])
        replacements = {
            str(item["operator_id"]): item
            for item in spec.get("mutation_source_replacements", ())
        }
        for operator_id_value in spec.get("mutation_operator_ids", ()):
            operator_id = str(operator_id_value)
            replacement = replacements.get(operator_id)
            assert replacement is not None
            semantic = (
                "exact_source_replacement",
                str(replacement["path"]),
                str(replacement["old"]),
                str(replacement["new"]),
            )
            semantics_by_operator.setdefault(operator_id, set()).add(semantic)
            owners_by_operator.setdefault(operator_id, set()).add(requirement_id)

    collisions = {
        operator_id: {
            "requirements": sorted(owners_by_operator[operator_id]),
            "semantic_count": len(semantics),
        }
        for operator_id, semantics in semantics_by_operator.items()
        if len(semantics) != 1
    }
    assert collisions == {}


def test_phase1_governance_loader_never_follows_a_leaf_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = tmp_path / "counterfeit-governance.json"
    external.write_bytes(phase1_data._DATA_PATH.read_bytes())  # noqa: SLF001
    alias = tmp_path / "phase1_governance_data.json"
    alias.symlink_to(external)
    monkeypatch.setattr(phase1_data, "_DATA_PATH", alias)

    with pytest.raises(RuntimeError, match="phase1 governance data is unavailable"):
        phase1_data._load()  # noqa: SLF001


def test_phase1_governance_loader_rejects_duplicate_source_replacement() -> None:
    payload = copy.deepcopy(phase1_data._DATA)  # noqa: SLF001
    spec = next(
        item
        for item in payload["PHASE1_ROOT_REQUIREMENT_SPECS"]
        if item.get("mutation_source_replacements")
    )
    spec["mutation_source_replacements"] = (
        *spec["mutation_source_replacements"],
        spec["mutation_source_replacements"][0],
    )

    with pytest.raises(
        RuntimeError,
        match="source mutation denominator contains duplicates",
    ):
        phase1_data._validate_payload(payload)  # noqa: SLF001


def test_phase1_governance_loader_rejects_operator_without_exact_source_mutation() -> None:
    payload = copy.deepcopy(phase1_data._DATA)  # noqa: SLF001
    spec = next(
        item
        for item in payload["PHASE1_ROOT_REQUIREMENT_SPECS"]
        if len(item.get("mutation_source_replacements", ())) > 1
    )
    removed = spec["mutation_source_replacements"][-1]
    spec["mutation_source_replacements"] = spec["mutation_source_replacements"][:-1]
    requirement_id = str(spec["requirement_id"])
    explicit = payload["PHASE1_EXPLICIT_SOURCE_MUTATIONS"][requirement_id]
    explicit_items = explicit if isinstance(explicit, tuple) else (explicit,)
    payload["PHASE1_EXPLICIT_SOURCE_MUTATIONS"][requirement_id] = tuple(
        item
        for item in explicit_items
        if item["operator_id"] != removed["operator_id"]
    )

    with pytest.raises(
        RuntimeError,
        match="source mutation operator denominator is incomplete",
    ):
        phase1_data._validate_payload(payload)  # noqa: SLF001


def test_phase1_governance_loader_rejects_duplicate_candidate_path() -> None:
    payload = copy.deepcopy(phase1_data._DATA)  # noqa: SLF001
    spec = payload["PHASE1_ROOT_REQUIREMENT_SPECS"][0]
    spec["candidate_paths"] = (
        *spec["candidate_paths"],
        spec["candidate_paths"][0],
    )

    with pytest.raises(
        RuntimeError,
        match="candidate path denominator is invalid",
    ):
        phase1_data._validate_payload(payload)  # noqa: SLF001


def test_phase1_governance_loader_rejects_duplicate_mutation_candidate_path() -> None:
    payload = copy.deepcopy(phase1_data._DATA)  # noqa: SLF001
    spec = payload["PHASE1_ROOT_REQUIREMENT_SPECS"][0]
    spec["mutation_candidate_paths"] = (
        *spec["mutation_candidate_paths"],
        spec["mutation_candidate_paths"][0],
    )

    with pytest.raises(
        RuntimeError,
        match="mutation path denominator is invalid",
    ):
        phase1_data._validate_payload(payload)  # noqa: SLF001


def test_phase1_governance_loader_rejects_duplicate_explicit_mutation() -> None:
    payload = copy.deepcopy(phase1_data._DATA)  # noqa: SLF001
    requirement_id, value = next(
        (requirement_id, value)
        for requirement_id, value in payload[
            "PHASE1_EXPLICIT_SOURCE_MUTATIONS"
        ].items()
        if value
    )
    items = value if isinstance(value, tuple) else (value,)
    payload["PHASE1_EXPLICIT_SOURCE_MUTATIONS"][requirement_id] = (
        *items,
        items[0],
    )

    with pytest.raises(
        RuntimeError,
        match="explicit source mutation denominator contains duplicates",
    ):
        phase1_data._validate_payload(payload)  # noqa: SLF001


def test_phase1_governance_loader_rejects_structural_denominator_drift() -> None:
    def first_spec(payload):
        return payload["PHASE1_ROOT_REQUIREMENT_SPECS"][0]

    def noncanonical_candidate_sequence(payload):
        spec = first_spec(payload)
        spec["candidate_paths"] = list(spec["candidate_paths"])

    def mutation_path_outside_candidate(payload):
        spec = first_spec(payload)
        spec["mutation_candidate_paths"] = (
            *spec["mutation_candidate_paths"],
            "outside/governed/candidate.py",
        )

    def duplicate_mapped_oracle(payload):
        spec = first_spec(payload)
        operator_id = spec["mutation_operator_ids"][0]
        mapped = spec["mutation_oracle_node_ids_by_operator"][operator_id]
        duplicated = (*mapped, mapped[0])
        spec["mutation_oracle_node_ids_by_operator"][operator_id] = duplicated
        payload["PHASE1_MUTATION_ORACLE_NODES"][spec["requirement_id"]][
            operator_id
        ] = duplicated

    def invalid_singular_projection(payload):
        spec = next(
            item
            for item in payload["PHASE1_ROOT_REQUIREMENT_SPECS"]
            if "mutation_source_replacement" in item
        )
        spec["mutation_source_replacement"] = "not-a-mutation-object"

    def unknown_empty_explicit_owner(payload):
        payload["PHASE1_EXPLICIT_SOURCE_MUTATIONS"]["UNKNOWN-OWNER"] = ()

    def duplicate_revalidation_record(payload):
        payload["PHASE1_REVALIDATION_SEQUENCE"] = (
            *payload["PHASE1_REVALIDATION_SEQUENCE"],
            payload["PHASE1_REVALIDATION_SEQUENCE"][0],
        )

    def reordered_root_contract(payload):
        specs = list(payload["PHASE1_ROOT_REQUIREMENT_SPECS"])
        first_root = next(
            index
            for index, spec in enumerate(specs)
            if spec["requirement_id"].startswith("ROOT-")
        )
        second_root = next(
            index
            for index in range(first_root + 1, len(specs))
            if specs[index]["requirement_id"].startswith("ROOT-")
        )
        specs[first_root], specs[second_root] = specs[second_root], specs[first_root]
        payload["PHASE1_ROOT_REQUIREMENT_SPECS"] = tuple(specs)

    def empty_closure_boundaries(payload):
        payload["PHASE1_CLOSURE_BOUNDARIES"] = {}

    def unknown_boundary_override(payload):
        existing = next(
            iter(payload["PHASE1_REVALIDATION_BOUNDARY_OVERRIDES"].values())
        )
        payload["PHASE1_REVALIDATION_BOUNDARY_OVERRIDES"]["unknown-record"] = existing

    def empty_phase0_support(payload):
        payload["PHASE0_SUPPORT_REQUIREMENT_SPECS"] = ()

    corruptions = (
        noncanonical_candidate_sequence,
        mutation_path_outside_candidate,
        duplicate_mapped_oracle,
        invalid_singular_projection,
        unknown_empty_explicit_owner,
        duplicate_revalidation_record,
        reordered_root_contract,
        empty_closure_boundaries,
        unknown_boundary_override,
        empty_phase0_support,
    )
    accepted = []
    for corrupt in corruptions:
        payload = copy.deepcopy(phase1_data._DATA)  # noqa: SLF001
        corrupt(payload)
        try:
            phase1_data._validate_payload(payload)  # noqa: SLF001
        except RuntimeError:
            continue
        accepted.append(corrupt.__name__)

    assert accepted == []


def test_phase1_mutation_oracles_are_in_the_candidate_node_denominator() -> None:
    """A mutation kill cannot receive credit outside its governed candidate run."""

    missing = {
        str(spec["requirement_id"]): sorted(
            set(spec.get("mutation_oracle_node_ids", ()))
            - set(spec.get("node_ids", ()))
        )
        for spec in governance.PHASE1_ROOT_REQUIREMENT_SPECS
        if set(spec.get("mutation_oracle_node_ids", ()))
        - set(spec.get("node_ids", ()))
    }

    assert missing == {}


def test_phase1_governance_loader_rejects_oracle_outside_candidate_denominator() -> None:
    payload = copy.deepcopy(phase1_data._DATA)  # noqa: SLF001
    specs = list(payload["PHASE1_ROOT_REQUIREMENT_SPECS"])
    spec = dict(specs[0])
    spec["node_ids"] = tuple(
        node_id
        for node_id in spec["node_ids"]
        if node_id != spec["mutation_oracle_node_ids"][0]
    )
    specs[0] = spec
    payload["PHASE1_ROOT_REQUIREMENT_SPECS"] = tuple(specs)

    with pytest.raises(
        RuntimeError,
        match="requirement contract is invalid",
    ):
        phase1_data._validate_payload(payload)  # noqa: SLF001


def test_phase1_requirement_summary_rejects_population_policy_drift() -> None:
    summary = governance.phase1_requirement_revalidation_summary()
    current = {"requirement_revalidation": summary}

    assert governance._phase1_requirement_revalidation_is_current(  # noqa: SLF001
        current
    )

    drifted = json.loads(json.dumps(current))
    drifted["requirement_revalidation"]["population_policy_by_requirement"][
        "ROOT-COG-045"
    ] = "all_exact_nodes_required_no_skip_no_xfail"
    assert not governance._phase1_requirement_revalidation_is_current(  # noqa: SLF001
        drifted
    )


def test_empty_pytest_body_cannot_satisfy_a_phase0_oracle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    oracle = tmp_path / "tests" / "test_empty_oracle.py"
    oracle.parent.mkdir()
    oracle.write_text("def test_empty_oracle():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(governance, "ROOT", tmp_path)

    node_id = "tests/test_empty_oracle.py::test_empty_oracle"

    assert governance._pytest_node_exists(node_id) is True
    assert governance._pytest_node_has_assertion(node_id) is False

    oracle.write_text(
        "import pytest\n"
        "@pytest.mark.skip(reason='hidden')\n"
        "def test_empty_oracle():\n"
        "    assert 1 == 1\n",
        encoding="utf-8",
    )

    assert governance._pytest_node_outcome_markers(node_id) == {"skip"}

    oracle.write_text(
        "def test_empty_oracle():\n    assert True\n",
        encoding="utf-8",
    )

    assert governance._pytest_node_has_assertion(node_id) is False


def test_phase0_support_requirement_specs_cannot_self_certify_after_deletion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _materialize(tmp_path, monkeypatch)
    monkeypatch.setattr(
        governance,
        "PHASE0_SUPPORT_REQUIREMENT_SPECS",
        governance.PHASE0_SUPPORT_REQUIREMENT_SPECS[:-1],
    )
    for path, content in governance.build_assets().items():
        path.write_text(content, encoding="utf-8")

    errors = governance.validate_assets()

    assert "independent governed hash mismatch: phase0_support_requirement_specs" in errors
    assert "Phase 0 support-WP requirement coverage is incomplete" in errors


def test_artifact_and_certificate_deletion_is_blocking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    acceptance = _materialize(tmp_path, monkeypatch)
    for filename, key in (
        ("audit_artifact_registry.json", "artifacts"),
        ("cognitive_release_manifest.json", "certificates"),
    ):
        path = acceptance / filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload[key].pop()
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert f"stale generated asset: {filename}" in governance.validate_assets()
        path.write_text(governance.build_assets()[path], encoding="utf-8")


def test_closure_index_hash_binds_generated_jsonl() -> None:
    index = json.loads(
        (governance.ACCEPTANCE / "cognitive_root_closure_index.json").read_text(encoding="utf-8")
    )
    rows = (governance.ACCEPTANCE / "cognitive_root_closures.jsonl").read_text(encoding="utf-8")

    assert index["root_count"] == 50
    assert len(rows.splitlines()) == 50
    assert index["closure_jsonl_sha256"] == governance.hashlib.sha256(rows.encode()).hexdigest()


def test_closure_projection_sources_have_one_active_contract_owner() -> None:
    rows = [
        json.loads(row)
        for row in (governance.ACCEPTANCE / "cognitive_root_closures.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert len(rows) == 50
    assert {row["schema_version"] for row in rows} == {
        "mnemos.cognitive_root_closure_projection.v2"
    }
    assert {row["source_asset_id"] for row in rows} == {
        "desktop:mnemos-phase0-7-global-contract-2026-07-24"
    }
    assert {row["source_record"] for row in rows} == {
        "Desktop/Mnemos-Phase0-7全局工程修复合同-2026-07-24.md"
    }
    assert {
        (
            row["source_anchor"]["asset_id"],
            row["source_anchor"]["start"],
            row["source_anchor"]["end"],
        )
        for row in rows
    } == {
        (
            "desktop:mnemos-phase0-7-global-contract-2026-07-24",
            "### 14.7",
            "### 14.8",
        )
    }


@requires_desktop_governance
def test_active_contract_imports_exact_50_root_definitions() -> None:
    imported = governance._imported_root_definition_ids(governance.GOVERNING_CONTRACT_PATH)

    assert len(imported) == 50
    assert set(imported) == {root_id for root_id, _ in governance.ROOT_ORDER}


def test_missing_current_governing_contract_is_blocking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        governance,
        "GOVERNING_CONTRACT_PATH",
        tmp_path / "missing-current-contract.md",
    )

    assert "missing current governing contract" in governance.validate_assets()


def test_current_contract_bytes_outside_governed_sections_do_not_create_hash_cycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    contract_path = tmp_path / "current-contract.md"
    contract_path.write_text(
        "outside-before\n"
        "### 14.7\nroot contract\n"
        "### 14.8\nfinding contract\n"
        "### 14.9\noutside-after\n",
        encoding="utf-8",
    )
    independent = json.loads(governance.INDEPENDENT_DENOMINATOR_PATH.read_text(encoding="utf-8"))
    independent["governing_source"]["section_14_7_sha256"] = governance._section_sha256(
        contract_path,
        "### 14.7",
        "### 14.8",
    )
    independent["governing_source"]["section_14_8_sha256"] = governance._section_sha256(
        contract_path,
        "### 14.8",
        "### 14.9",
    )
    independent_path = tmp_path / "independent.json"
    independent_path.write_text(json.dumps(independent), encoding="utf-8")
    monkeypatch.setattr(governance, "INDEPENDENT_DENOMINATOR_PATH", independent_path)
    monkeypatch.setattr(governance, "GOVERNING_CONTRACT_PATH", contract_path)

    errors = governance.validate_assets()

    assert "stale COG-046 Desktop evidence: handoff_sha256" not in errors
    assert not any(error.startswith("governing source section drift:") for error in errors)


def test_current_desktop_final_byte_hash_is_detached_only() -> None:
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    historical_record = ledger["phase0_cog046_denominator_lock_20260724"]
    record = ledger["contract_governance_migration_20260724"]
    authority = record["contract_authority"]
    manifest = json.loads(
        (governance.ACCEPTANCE / "document_asset_manifest.json").read_text(encoding="utf-8")
    )

    assert "desktop_sync" in historical_record
    assert "desktop_sync" not in record
    assert authority["active_final_byte_hash_owner"] == "detached_closure_bundle_only"
    assert "active_sha256" not in authority
    assert "frozen_sha256" not in manifest["external_governing_assets"][0]
    assert manifest["external_historical_assets"][0]["frozen_sha256"].startswith("sha256:")


def test_contract_governance_uses_append_only_evidence_generation() -> None:
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    independent = json.loads(governance.INDEPENDENT_DENOMINATOR_PATH.read_text(encoding="utf-8"))
    migration = ledger["contract_governance_migration_20260724"]
    historical_hashes = independent["historical_evidence_hashes"]

    for record_id, expected_hash in historical_hashes.items():
        assert governance._hash(ledger[record_id]) == expected_hash
    assert migration["supersedes_evidence_record"] == "phase0_cog046_denominator_lock_20260724"
    assert migration["projection_generation"]["previous"] == {
        "schema_version": "mnemos.cognitive_root_closure_projection.v1",
        "count": 50,
        "jsonl_sha256": "322d24b6b49893c3bfec1b4441c365ac7afd8da3cd4d2600cbf323e57ed73a94",
        "index_sha256": "9f6389012664133b92280d016eb21af2caf2e95151726c3eb1d159ad0d50c364",
    }
    assert migration["projection_generation"]["current"]["schema_version"] == (
        "mnemos.cognitive_root_closure_projection.v2"
    )
    assert ledger["current_evidence_generation"] == "phase0_cog046_followup_repair_20260725"


def test_phase1_historical_baseline_is_git_anchored_and_redacted() -> None:
    ledger = json.loads(governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8"))

    assert governance._validate_phase1_historical_artifacts(ledger) == []


def test_phase1_historical_baseline_rejects_mutated_git_blob(monkeypatch) -> None:
    ledger = json.loads(governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8"))
    original = governance._git_blob_bytes
    historical = governance.PHASE1_IMMUTABLE_HISTORICAL_ARTIFACTS[
        "cog008_review_baseline_v1"
    ]

    def mutated_blob(commit: str, relative_path: str) -> bytes | None:
        if (
            commit == historical["implementation_commit"]
            and relative_path == historical["path"]
        ):
            return b'{"mutated":true}\n'
        return original(commit, relative_path)

    monkeypatch.setattr(governance, "_git_blob_bytes", mutated_blob)

    assert (
        "immutable COG-008 historical Git blob mismatch"
        in governance._validate_phase1_historical_artifacts(ledger)
    )


def test_phase1_historical_baseline_rejects_missing_current_supersession() -> None:
    ledger = json.loads(governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8"))
    latest_record_id = governance.PHASE1_REVALIDATION_SEQUENCE[-1][1]
    current_record = next(
        value
        for value in reversed(tuple(ledger.values()))
        if isinstance(value, dict)
        and "historical_evidence_supersession" in value
    )
    ledger[latest_record_id] = dict(current_record)
    ledger[latest_record_id].pop("historical_evidence_supersession", None)

    assert (
        "current Phase 1 historical evidence supersession binding mismatch"
        in governance._validate_phase1_historical_artifacts(ledger)
    )


def test_phase0_followup_full_residual_has_an_exact_phase5_owner() -> None:
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    record = ledger["phase0_cog046_followup_repair_20260725"]

    assert record["supersedes_evidence_record"] == ("phase0_cog040_followup_revalidation_20260725")
    assert record["residual_dispositions"] == [
        {
            "failed_node": (
                "tests/integration/test_wiki_read_authorization.py::"
                "test_same_agent_private_page_remains_readable"
            ),
            "failure": (
                "SignalStore is uninitialized; use an explicit bootstrap or "
                "reconciliation command"
            ),
            "root_cause": (
                "authorized wiki_read unconditionally attempts persona SignalStore "
                "knowledge-signal persistence before that canonical store is initialized"
            ),
            "owner_root_id": "COG-020",
            "owner_phase_order": "P5-05",
            "finding_ids": ["P5-003", "P5-006"],
            "status": "DEFERRED_TO_OWNING_ROOT",
            "next_allowed": "Phase 5 COG-020",
            "invalidates": ["COG-021", "COG-024", "COG-042"],
            "phase0_action": (
                "retain the failing test and fail-closed store boundary; do not seed, "
                "bootstrap, swallow, or mutate production state"
            ),
        }
    ]


def test_cog025_revalidation_is_superseded_by_the_current_phase1_generation() -> None:
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    independent = json.loads(governance.INDEPENDENT_DENOMINATOR_PATH.read_text(encoding="utf-8"))
    historical = ledger["phase0_cog025_evidence_revalidation_20260724"]
    record = ledger["phase0_cog025_followup_repair_20260725"]

    assert (
        governance._hash(historical)
        == independent["superseded_phase0_generation_hashes"][
            "phase0_cog025_evidence_revalidation_20260724"
        ]
    )
    assert record["supersedes_evidence_record"] == "phase0_cog046_gate_hardening_20260724"
    phase1 = json.loads(governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8"))
    current = phase1[governance.PHASE1_REVALIDATION_SEQUENCE[-1][1]]
    for artifact in current["artifacts"].values():
        path = governance.ROOT / artifact["path"]
        assert governance.hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    assert record["closure_boundary"] == {
        "historical_closure_record_rewritten": False,
        "production_effect": "not reverified",
        "production_mutation": "not authorized and not performed",
        "readiness_certified": False,
        "release_eligible": False,
    }


def test_governance_rejects_stale_cog025_revalidation_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    ledger["phase0_cog025_evidence_revalidation_20260724"]["direct_path"]["sha256"] = "0" * 64
    ledger_path = tmp_path / "stale-cog025-evidence-ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(governance, "PHASE0_LEDGER_PATH", ledger_path)

    assert (
        "superseded Phase 0 generation drift: " "phase0_cog025_evidence_revalidation_20260724"
    ) in governance.validate_assets()


def test_cog040_revalidation_preserves_the_frozen_baseline_boundary() -> None:
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    record = ledger["phase0_cog040_contract_revalidation_20260724"]
    artifact = record["baseline_contract"]
    path = governance.ROOT / artifact["path"]

    assert record["supersedes_evidence_record"] == "phase0_cog025_evidence_revalidation_20260724"
    assert governance.hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    assert record["closure_boundary"] == {
        "baseline_semantics_changed": False,
        "performance_certificate_eligible": False,
        "phase7_root_closed": False,
        "production_mutation": "not authorized and not performed",
        "release_eligible": False,
    }


def test_cog046_followup_uses_a_current_append_only_evidence_generation() -> None:
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    independent = json.loads(governance.INDEPENDENT_DENOMINATOR_PATH.read_text(encoding="utf-8"))
    historical = ledger["phase0_cog046_gate_hardening_20260724"]
    record = ledger["phase0_cog046_followup_repair_20260725"]

    assert (
        governance._hash(historical)
        == independent["superseded_phase0_generation_hashes"][
            "phase0_cog046_gate_hardening_20260724"
        ]
    )
    assert record["supersedes_evidence_record"] == ("phase0_cog040_followup_revalidation_20260725")
    assert (
        governance._hash(record)
        == independent["superseded_phase0_generation_hashes"][
            "phase0_cog046_followup_repair_20260725"
        ]
    )
    phase1 = json.loads(governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8"))
    current = phase1[governance.PHASE1_REVALIDATION_SEQUENCE[-1][1]]
    for artifact in current["artifacts"].values():
        path = governance.ROOT / artifact["path"]
        assert governance.hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    assert record["closure_boundary"] == {
        "cog046_phase7_closed": False,
        "production_effect": "not verified",
        "production_mutation": "not authorized and not performed",
        "readiness_certified": False,
        "release_eligible": False,
    }


def test_phase1_cog045_has_append_only_shared_governance_revalidation() -> None:
    ledger = json.loads(governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8"))
    record = ledger["phase1_cog045_governance_revalidation_20260725"]

    assert record["supersedes_evidence_record"] == ("phase1_cog045_contract_revalidation_20260725")
    assert record["root_id"] == "COG-045"
    assert record["closure_boundary"] == {
        "code_contract_verified": True,
        "live_cursor_schema_migrated": False,
        "live_snapshot_raw_rebuilt": False,
        "next_root_started": False,
        "production_effect": "not verified",
        "production_mutation": "not authorized and not performed",
        "readiness_certified": False,
        "release_eligible": False,
        "root_closed": False,
    }


def test_phase1_cog045_requirements_are_registered_to_exact_oracles() -> None:
    manifest = json.loads(
        (governance.ACCEPTANCE / "cognitive_requirement_test_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    requirements = {
        item["requirement_id"]: item
        for item in manifest["requirements"]
        if item["root_id"] == "COG-045"
    }

    assert set(requirements) == {
        "ROOT-COG-045",
        "FINDING-P12-004-COG-045",
    }
    assert all(item["status"] == "REGISTERED" for item in requirements.values())
    assert all(item["coverage_scope"] == "phase1_root_revalidation" for item in requirements.values())
    assert all(item["node_ids"] for item in requirements.values())


def test_phase1_cog045_closure_projection_is_current_but_not_live_closed() -> None:
    independent = json.loads(
        governance.INDEPENDENT_DENOMINATOR_PATH.read_text(encoding="utf-8")
    )
    ledger = json.loads(
        governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8")
    )
    rows = [
        json.loads(row)
        for row in (governance.ACCEPTANCE / "cognitive_root_closures.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    row = next(item for item in rows if item["root_id"] == "COG-045")
    latest_key = next(
        evidence_key
        for root_id, evidence_key in reversed(
            governance.PHASE1_REVALIDATION_SEQUENCE
        )
        if root_id == "COG-045"
    )
    current = ledger[latest_key]

    assert row["state"] == independent["closure_states"]["COG-045"]
    assert row["state"] == "CODE_CONTRACT_REVALIDATED_LIVE_RAW_REBUILD_PENDING"
    assert row["machine_artifact"].endswith("#" + latest_key)
    assert current["closure_boundary"]["live_cursor_schema_migrated"] is True
    assert current["closure_boundary"]["live_snapshot_raw_rebuilt"] is False
    assert current["closure_boundary"]["root_closed"] is False
    assert current["closure_boundary"]["release_eligible"] is False
    assert (
        governance._hash(current["closure_boundary"])
        == independent["closure_boundary_hashes"]["COG-045"]
    )
    assert row["machine_evidence_hash"]


def test_phase1_revalidation_sequence_is_serial_and_projected() -> None:
    ledger = json.loads(governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8"))
    requirements = json.loads(
        (governance.ACCEPTANCE / "cognitive_requirement_test_manifest.json").read_text(
            encoding="utf-8"
        )
    )["requirements"]
    closures = {
        item["root_id"]: item
        for item in map(
            json.loads,
            (governance.ACCEPTANCE / "cognitive_root_closures.jsonl")
            .read_text(encoding="utf-8")
            .splitlines(),
        )
    }
    previous = "phase1_cog045_migration_contract_completion_20260725"
    latest_evidence_by_root = {
        root_id: evidence_key
        for root_id, evidence_key in governance.PHASE1_REVALIDATION_SEQUENCE
    }

    for root_id, evidence_key in governance.PHASE1_REVALIDATION_SEQUENCE:
        record = ledger[evidence_key]
        assert record["root_id"] == root_id
        assert record.get(
            "sequence_predecessor",
            record["supersedes_evidence_record"],
        ) == previous
        assert record["closure_boundary"]["root_closed"] is False
        assert record["closure_boundary"]["production_effect"] == "not verified"
        if latest_evidence_by_root[root_id] == evidence_key:
            assert closures[root_id]["state"] == record["state"]
            assert closures[root_id]["machine_artifact"].endswith("#" + evidence_key)
        else:
            assert not closures[root_id]["machine_artifact"].endswith("#" + evidence_key)
        root_requirements = [
            item
            for item in requirements
            if item["root_id"] == root_id
            and item.get("coverage_scope") == "phase1_root_revalidation"
        ]
        assert root_requirements
        assert all(item["status"] == "REGISTERED" for item in root_requirements)
        previous = evidence_key

    cog009_overlay = ledger["phase1_cog009_projection_reference_overlay_20260725"]
    assert (
        cog009_overlay["supersedes_evidence_record"]
        == "phase1_cog009_contract_revalidation_20260725"
    )


def test_cog001_live_soak_overclaim_is_blocking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger = json.loads(governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8"))
    ledger["phase1_cog001_contract_revalidation_20260725"]["closure_boundary"][
        "live_two_poll_soak_verified"
    ] = True
    ledger_path = tmp_path / "cog001-live-soak-overclaim.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(governance, "PHASE1_LEDGER_PATH", ledger_path)

    errors = governance.validate_assets(desktop_mode="skip")

    assert (
        "invalid Phase 1 revalidation sequence: "
        "phase1_cog001_contract_revalidation_20260725"
    ) in errors


def test_governance_rejects_stale_cog046_hardening_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    ledger["phase0_cog046_gate_hardening_20260724"]["artifacts"]["gate_execution"]["sha256"] = (
        "0" * 64
    )
    ledger_path = tmp_path / "stale-cog046-evidence-ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(governance, "PHASE0_LEDGER_PATH", ledger_path)

    assert (
        "superseded Phase 0 generation drift: " "phase0_cog046_gate_hardening_20260724"
    ) in governance.validate_assets()


def test_current_closure_projection_points_to_latest_phase0_evidence() -> None:
    rows = [
        json.loads(row)
        for row in (governance.ACCEPTANCE / "cognitive_root_closures.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    expected_generations = {
        "COG-025": "phase0_cog025_followup_repair_20260725",
        "COG-040": "phase0_cog040_followup_revalidation_20260725",
        "COG-046": "phase0_cog046_followup_repair_20260725",
    }

    for root_id, generation in expected_generations.items():
        row = next(item for item in rows if item["root_id"] == root_id)
        assert row["machine_artifact"].endswith("#" + generation)


def test_current_projection_hash_binds_revalidation_verification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    current_rows = [
        json.loads(row)
        for row in (governance.ACCEPTANCE / "cognitive_root_closures.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    current_cog025 = next(row for row in current_rows if row["root_id"] == "COG-025")
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    ledger["phase0_cog025_followup_repair_20260725"]["verification"][
        "focused"
    ] = "failed-but-hidden"
    ledger_path = tmp_path / "mutated-verification-ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(governance, "PHASE0_LEDGER_PATH", ledger_path)

    regenerated_rows = [
        json.loads(row)
        for row in governance.build_assets()[
            governance.ACCEPTANCE / "cognitive_root_closures.jsonl"
        ].splitlines()
    ]
    regenerated_cog025 = next(row for row in regenerated_rows if row["root_id"] == "COG-025")

    assert regenerated_cog025["machine_evidence_hash"] != (current_cog025["machine_evidence_hash"])


def test_unknown_superseding_phase0_generation_is_blocking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    ledger["phase0_unreviewed_release_overclaim_20260724"] = {
        "record_type": "append_only_phase0_release_generation",
        "supersedes_evidence_record": "phase0_cog046_gate_hardening_20260724",
        "phase7_root_closed": True,
        "production_effect": "verified",
        "release_eligible": True,
    }
    ledger["current_evidence_generation"] = "phase0_unreviewed_release_overclaim_20260724"
    ledger_path = tmp_path / "overclaim-generation-ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(governance, "PHASE0_LEDGER_PATH", ledger_path)

    errors = governance.validate_assets()
    assert "unknown append-only Phase 0 evidence generation" in errors
    assert "current Phase 0 evidence generation mismatch" in errors


def test_known_phase0_generation_rejects_top_level_release_overclaim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    record = ledger["phase0_cog046_gate_hardening_20260724"]
    record["phase7_root_closed"] = True
    record["production_effect"] = "verified"
    record["release_eligible"] = True
    ledger_path = tmp_path / "known-generation-overclaim-ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(governance, "PHASE0_LEDGER_PATH", ledger_path)

    errors = governance.validate_assets()
    assert "COG-046 evidence generation schema mismatch" in errors
    assert "Phase 0 evidence generation overclaim" in errors


def test_nonstandard_unknown_ledger_generation_is_blocking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    ledger["unreviewed_release_generation_20260724"] = {
        "record_type": "append_only_release_generation",
        "supersedes_evidence_record": "phase0_cog046_gate_hardening_20260724",
        "phase7_root_closed": True,
        "production_effect": "verified",
        "release_eligible": True,
    }
    ledger_path = tmp_path / "nonstandard-overclaim-generation-ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(governance, "PHASE0_LEDGER_PATH", ledger_path)

    assert "unexpected Phase 0 ledger top-level keys" in governance.validate_assets()


def test_legacy_ledger_contract_references_are_explicitly_historical() -> None:
    for path in (governance.PHASE0_LEDGER_PATH, governance.PHASE1_LEDGER_PATH):
        ledger = json.loads(path.read_text(encoding="utf-8"))
        resolution = ledger["contract_authority_resolution_20260724"]
        assert resolution == {
            "record_type": "append_only_authority_resolution",
            "legacy_field": "audit_contract.desktop_report",
            "legacy_evidence_role": "historical_snapshot_not_current_gate",
            "legacy_asset_id": governance.HISTORICAL_SOURCE_ASSET_ID,
            "current_active_asset_id": governance.GOVERNING_CONTRACT_ASSET_ID,
            "current_active_path": governance.GOVERNING_CONTRACT_PATH.name,
            "gate_owner": "document_asset_manifest.external_governing_assets",
        }


@requires_desktop_governance
def test_imported_contract_corpus_is_deterministically_equal_to_frozen_history() -> None:
    assert governance._imported_corpus_matches_historical(
        governance.GOVERNING_CONTRACT_PATH,
        governance.HISTORICAL_SOURCE_PATH,
    )


def test_renamed_handoff_predecessor_is_non_gating_and_hash_chained() -> None:
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(
        (governance.ACCEPTANCE / "document_asset_manifest.json").read_text(encoding="utf-8")
    )
    predecessor = manifest["external_governing_assets"][0]["renamed_from"]

    assert predecessor == {
        "asset_id": governance.GOVERNING_CONTRACT_PREDECESSOR_ASSET_ID,
        "path": governance.GOVERNING_CONTRACT_PREDECESSOR_PATH.name,
        "sha256": "sha256:"
        + ledger["phase0_cog046_denominator_lock_20260724"]["desktop_sync"]["handoff_sha256"],
    }
    assert not governance.GOVERNING_CONTRACT_PREDECESSOR_PATH.exists()


@requires_desktop_governance
def test_imported_corpus_cannot_self_certify_after_content_loss(
    tmp_path: Path,
    monkeypatch,
) -> None:
    contract_path = tmp_path / governance.GOVERNING_CONTRACT_PATH.name
    text = governance.GOVERNING_CONTRACT_PATH.read_text(encoding="utf-8")
    contract_path.write_text(text.replace("目标不是", "目标也不是", 1), encoding="utf-8")
    independent = json.loads(governance.INDEPENDENT_DENOMINATOR_PATH.read_text(encoding="utf-8"))
    independent["governing_source"]["imported_contract_corpus_sha256"] = governance._section_sha256(
        contract_path,
        governance.IMPORTED_CORPUS_START,
        governance.IMPORTED_CORPUS_END,
    )
    independent_path = tmp_path / "self-signed-independent.json"
    independent_path.write_text(json.dumps(independent), encoding="utf-8")
    monkeypatch.setattr(governance, "GOVERNING_CONTRACT_PATH", contract_path)
    monkeypatch.setattr(governance, "INDEPENDENT_DENOMINATOR_PATH", independent_path)

    assert "imported contract corpus is not equivalent to frozen historical source" in (
        governance.validate_assets()
    )


def test_ledger_authority_resolution_is_required_for_every_legacy_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    phase1 = json.loads(governance.PHASE1_LEDGER_PATH.read_text(encoding="utf-8"))
    phase1.pop("contract_authority_resolution_20260724")
    phase1_path = tmp_path / "phase1-ledger.json"
    phase1_path.write_text(json.dumps(phase1), encoding="utf-8")
    monkeypatch.setattr(governance, "PHASE1_LEDGER_PATH", phase1_path)

    assert "missing legacy audit contract authority resolution" in governance.validate_assets()


def test_predecessor_provenance_cannot_self_certify_in_both_manifests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    acceptance = _materialize(tmp_path, monkeypatch)
    manifest_path = acceptance / "document_asset_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["external_governing_assets"][0]["renamed_from"]["sha256"] = "sha256:" + ("0" * 64)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    independent = json.loads(governance.INDEPENDENT_DENOMINATOR_PATH.read_text(encoding="utf-8"))
    independent["external_governing_assets"] = manifest["external_governing_assets"]
    independent_path = tmp_path / "self-signed-predecessor.json"
    independent_path.write_text(json.dumps(independent), encoding="utf-8")
    monkeypatch.setattr(governance, "INDEPENDENT_DENOMINATOR_PATH", independent_path)

    assert "renamed predecessor provenance mismatch" in governance.validate_assets()


def test_current_contract_governed_section_drift_is_blocking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    contract_path = tmp_path / "current-contract.md"
    contract_path.write_text(
        "### 14.7\nroot contract\n" "### 14.8\nfinding contract\n" "### 14.9\nend\n",
        encoding="utf-8",
    )
    independent = json.loads(governance.INDEPENDENT_DENOMINATOR_PATH.read_text(encoding="utf-8"))
    independent["governing_source"]["section_14_7_sha256"] = governance._section_sha256(
        contract_path,
        "### 14.7",
        "### 14.8",
    )
    independent["governing_source"]["section_14_8_sha256"] = governance._section_sha256(
        contract_path,
        "### 14.8",
        "### 14.9",
    )
    independent_path = tmp_path / "independent.json"
    independent_path.write_text(json.dumps(independent), encoding="utf-8")
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            "root contract",
            "mutated root contract",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(governance, "INDEPENDENT_DENOMINATOR_PATH", independent_path)
    monkeypatch.setattr(governance, "GOVERNING_CONTRACT_PATH", contract_path)

    assert "governing source section drift: section_14_7_sha256" in (governance.validate_assets())


def test_duplicate_governing_section_anchor_is_blocking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    contract_path = tmp_path / "ambiguous-contract.md"
    contract_path.write_text(
        "### 14.7\nfirst\n" "### 14.7\nsecond\n" "### 14.8\nfindings\n" "### 14.9\nend\n",
        encoding="utf-8",
    )
    independent = json.loads(governance.INDEPENDENT_DENOMINATOR_PATH.read_text(encoding="utf-8"))
    independent["governing_source"]["section_14_7_sha256"] = governance._section_sha256(
        contract_path,
        "### 14.7",
        "### 14.8",
    )
    independent["governing_source"]["section_14_8_sha256"] = governance._section_sha256(
        contract_path,
        "### 14.8",
        "### 14.9",
    )
    independent_path = tmp_path / "independent.json"
    independent_path.write_text(json.dumps(independent), encoding="utf-8")
    monkeypatch.setattr(governance, "INDEPENDENT_DENOMINATOR_PATH", independent_path)
    monkeypatch.setattr(governance, "GOVERNING_CONTRACT_PATH", contract_path)

    assert "governing source anchor cardinality mismatch" in governance.validate_assets()


def test_current_contract_identity_is_independently_bound(
    tmp_path: Path,
    monkeypatch,
) -> None:
    independent = json.loads(governance.INDEPENDENT_DENOMINATOR_PATH.read_text(encoding="utf-8"))
    independent["governing_source"]["asset_id"] = governance.HISTORICAL_SOURCE_ASSET_ID
    independent["governing_source"]["path"] = governance.HISTORICAL_SOURCE_PATH.name
    independent_path = tmp_path / "wrong-owner-independent.json"
    independent_path.write_text(json.dumps(independent), encoding="utf-8")
    monkeypatch.setattr(governance, "INDEPENDENT_DENOMINATOR_PATH", independent_path)

    assert "independent governing source identity mismatch" in governance.validate_assets()


def test_historical_source_contract_cannot_drift_from_independent_denominator(
    tmp_path: Path,
    monkeypatch,
) -> None:
    acceptance = _materialize(tmp_path, monkeypatch)
    manifest_path = acceptance / "document_asset_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["external_historical_assets"][0]["gate_eligible"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert "external historical document contract mismatch" in governance.validate_assets()


def test_two_active_contracts_cannot_self_certify_in_both_manifests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    acceptance = _materialize(tmp_path, monkeypatch)
    manifest_path = acceptance / "document_asset_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    duplicate = dict(manifest["external_governing_assets"][0])
    duplicate["asset_id"] = "desktop:second-active-contract"
    duplicate["path"] = "second-active-contract.md"
    manifest["external_governing_assets"].append(duplicate)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    independent = json.loads(governance.INDEPENDENT_DENOMINATOR_PATH.read_text(encoding="utf-8"))
    independent["external_governing_assets"] = manifest["external_governing_assets"]
    independent_path = tmp_path / "two-active-independent.json"
    independent_path.write_text(json.dumps(independent), encoding="utf-8")
    monkeypatch.setattr(governance, "INDEPENDENT_DENOMINATOR_PATH", independent_path)

    assert "external governing owner count must be exactly one" in governance.validate_assets()


def test_governing_constants_cannot_self_certify_after_regeneration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _materialize(tmp_path, monkeypatch)
    swapped = list(governance.ROOT_ORDER)
    swapped[2], swapped[3] = swapped[3], swapped[2]
    monkeypatch.setattr(governance, "ROOT_ORDER", tuple(swapped))
    for path, content in governance.build_assets().items():
        path.write_text(content, encoding="utf-8")

    assert "independent governed hash mismatch: root_order" in governance.validate_assets()


def test_owner_change_cannot_self_certify_after_regeneration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _materialize(tmp_path, monkeypatch)
    owners = dict(governance.FINDING_OWNERS)
    owners["P7-001"] = ("COG-041",)
    monkeypatch.setattr(governance, "FINDING_OWNERS", owners)
    for path, content in governance.build_assets().items():
        path.write_text(content, encoding="utf-8")

    assert "independent governed hash mismatch: finding_owners" in governance.validate_assets()


def test_discovered_inventory_and_full_score_gate_cannot_shrink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _materialize(tmp_path, monkeypatch)
    migrations = governance._migration_paths()
    monkeypatch.setattr(governance, "_migration_paths", lambda: migrations[:-1])
    gates = governance._full_score_gate_ids()
    monkeypatch.setattr(governance, "_full_score_gate_ids", lambda: gates[:-1])
    for path, content in governance.build_assets().items():
        path.write_text(content, encoding="utf-8")

    errors = governance.validate_assets()
    assert "independent inventory denominator mismatch: migration_paths" in errors
    assert "independent inventory denominator mismatch: full_score_gate_ids" in errors


def test_closure_projection_rejects_unapproved_ledger_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _materialize(tmp_path, monkeypatch)
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    ledger["phase0_cog040_baseline_20260724"]["state"] = "TAMPERED"
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(governance, "PHASE0_LEDGER_PATH", ledger_path)

    rows = governance.build_assets()[
        governance.ACCEPTANCE / "cognitive_root_closures.jsonl"
    ].splitlines()
    cog040 = next(json.loads(row) for row in rows if json.loads(row)["root_id"] == "COG-040")

    assert cog040["state"] == "INVALID_LEDGER_STATE"
    assert cog040["machine_evidence_hash"]
    assert "invalid Phase 0 closure ledger state: COG-040" in governance.validate_assets()


def test_closure_projection_rejects_release_boundary_overclaim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _materialize(tmp_path, monkeypatch)
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    boundary = ledger["phase0_cog046_denominator_lock_20260724"]["closure_boundary"]
    boundary["cog046_phase7_closed"] = True
    boundary["release_eligible"] = True
    boundary["production_effect"] = "verified"
    ledger_path = tmp_path / "overclaim-ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(governance, "PHASE0_LEDGER_PATH", ledger_path)
    for path, content in governance.build_assets().items():
        path.write_text(content, encoding="utf-8")

    assert "invalid Phase 0 closure ledger state: COG-046" in governance.validate_assets()


def test_governance_rejects_stale_ledger_denominator_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _materialize(tmp_path, monkeypatch)
    ledger = json.loads(governance.PHASE0_LEDGER_PATH.read_text(encoding="utf-8"))
    ledger["contract_governance_migration_20260724"]["denominators"]["requirement_test"][
        "count"
    ] = 50
    ledger_path = tmp_path / "stale-evidence-ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(governance, "PHASE0_LEDGER_PATH", ledger_path)
    for path, content in governance.build_assets().items():
        path.write_text(content, encoding="utf-8")

    assert (
        "superseded Phase 0 generation drift: contract_governance_migration_20260724"
        in governance.validate_assets()
    )


def test_requirement_and_release_denominators_are_complete() -> None:
    requirements = json.loads(
        (governance.ACCEPTANCE / "cognitive_requirement_test_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    for requirement in requirements["requirements"]:
        assert set(governance.REQUIREMENT_FIELDS) <= set(requirement)
    expected_pairs = {
        (finding_id, root_id)
        for finding_id, owners in governance.FINDING_OWNERS.items()
        for root_id in owners
    }
    assert len(expected_pairs) == 76
    finding_requirements = [
        item
        for item in requirements["requirements"]
        if item["requirement_kind"] == "finding_owner_coverage"
    ]
    assert {(item["finding_id"], item["root_id"]) for item in finding_requirements} == (
        expected_pairs
    )
    assert all({"T2", "T3"} <= set(item["test_lanes"]) for item in finding_requirements)
    phase0_finding_requirements = [
        item for item in finding_requirements if item.get("coverage_scope") == "phase0_support_wp"
    ]
    assert len(phase0_finding_requirements) == 11
    assert all(item["status"] == "REGISTERED" for item in phase0_finding_requirements)
    assert all(
        item["status"] == "UNREGISTERED"
        for item in finding_requirements
        if item.get("coverage_scope") not in {
            "phase0_support_wp",
            "phase1_root_revalidation",
        }
    )

    release = json.loads(
        (governance.ACCEPTANCE / "cognitive_release_manifest.json").read_text(encoding="utf-8")
    )
    gates = release["required_gate_denominator"]
    assert gates["gate_count"] == 63
    assert len(gates["gate_ids"]) == 63
    assert gates["runner"] == "scripts/run_full_score_gates.py"
    assert gates["verifier"] == "scripts/verify_full_score_certificate.py"


def test_phase1_schema_owners_are_registered_in_the_global_inventory() -> None:
    inventory = {
        str(item["path"]): item
        for item in governance._schema_inventory()  # noqa: SLF001
    }

    assert governance.PHASE1_REGISTERED_SCHEMA_OWNERS <= set(inventory)
    for path in governance.PHASE1_REGISTERED_SCHEMA_OWNERS:
        assert inventory[path]["owner_status"] == "REGISTERED"
        assert inventory[path]["release_blocking"] is False
    raw_index_owner = inventory["core/app/raw_search.py"]
    assert "raw_fts" in raw_index_owner["ddl_objects"]
    assert "CREATE VIRTUAL TABLE" in raw_index_owner["ddl_operations"]


def test_interface_and_certificate_identities_are_independently_bound(
    tmp_path: Path,
    monkeypatch,
) -> None:
    acceptance = _materialize(tmp_path, monkeypatch)
    interface_path = acceptance / "cognitive_runtime_interface_manifest.json"
    interfaces = json.loads(interface_path.read_text(encoding="utf-8"))
    interfaces["interfaces"][0]["interface_id"] = "replacement"
    interface_path.write_text(json.dumps(interfaces), encoding="utf-8")
    monkeypatch.setattr(
        governance,
        "CERTIFICATE_IDS",
        ("ReplacementCertificate", *governance.CERTIFICATE_IDS[1:]),
    )
    for path, content in governance.build_assets().items():
        path.write_text(content, encoding="utf-8")

    errors = governance.validate_assets()
    assert "independent identity denominator mismatch: runtime_interface_ids" in errors
    assert "independent identity denominator mismatch: certificate_ids" in errors
