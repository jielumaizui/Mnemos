from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from scripts.successor_d0_catalog import CatalogInputError, CatalogRequest, SuccessorD0Catalog
from scripts import successor_d0_verifier as verifier
from scripts.successor_d0_generation import snapshot as generator_snapshot
from scripts.successor_d0_verification import runner as verifier_runner
from scripts.successor_d0_verification import snapshot as verifier_snapshot
from scripts.successor_d0_verification import wire as verifier_wire

LEGACY_COMMIT = "1e36a31a26b0b5baf768815f185d57174e9c59dd"
TEST_DESIGN_TEXT = """# test successor design

<!-- accepted-principle:complete-function-denominator -->
<!-- accepted-principle:legacy-frozen-oracle-rollback -->
"""
EXPECTED_GENERATOR_IDENTITY_PATHS = [
    "scripts/generate_successor_d0_catalog.py",
    "scripts/successor_d0_catalog.py",
    "scripts/successor_d0_generation/__init__.py",
    "scripts/successor_d0_generation/builder.py",
    "scripts/successor_d0_generation/cli_inventory.py",
    "scripts/successor_d0_generation/contract_inventory.py",
    "scripts/successor_d0_generation/model.py",
    "scripts/successor_d0_generation/repository_inventory.py",
    "scripts/successor_d0_generation/runtime_inventory.py",
    "scripts/successor_d0_generation/snapshot.py",
    "scripts/successor_d0_generation/static_python.py",
]
EXPECTED_VERIFIER_IDENTITY_PATHS = [
    "scripts/audit_successor_d0_catalog.py",
    "scripts/successor_d0_verification/__init__.py",
    "scripts/successor_d0_verification/census.py",
    "scripts/successor_d0_verification/closure.py",
    "scripts/successor_d0_verification/runner.py",
    "scripts/successor_d0_verification/snapshot.py",
    "scripts/successor_d0_verification/wire.py",
    "scripts/successor_d0_verifier.py",
]


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _adversarial_archive_repo(
    tmp_path: Path,
    *,
    use_info_attributes: bool,
    attribute: str = "export-ignore",
) -> tuple[Path, str]:
    repo = tmp_path / ("info-attributes" if use_info_attributes else "nested-attributes")
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "d0-test@example.invalid")
    _git(repo, "config", "user.name", "D0 Test")
    nested = repo / "nested"
    nested.mkdir()
    (nested / "kept.txt").write_text("kept\n", encoding="utf-8")
    hidden_content = "$Format:%H$\n" if attribute == "export-subst" else "hidden\n"
    (nested / "hidden.txt").write_text(hidden_content, encoding="utf-8")
    if not use_info_attributes:
        (nested / ".gitattributes").write_text(
            f"hidden.txt {attribute}\n",
            encoding="utf-8",
        )
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "snapshot")
    commit = _git(repo, "rev-parse", "HEAD")
    if use_info_attributes:
        info_attributes = repo / ".git" / "info" / "attributes"
        info_attributes.write_text(
            f"nested/hidden.txt {attribute}\n",
            encoding="utf-8",
        )
    return repo, commit


def _surface_record() -> dict:
    return {
        "canonical_selector": "cli:status",
        "decision_ref": None,
        "discovery_key": "cli:status",
        "evidence_refs": [{"path": "mnemos_cli.py", "selector": "status"}],
        "facet_contract": {"kind": "command"},
        "input_contract_ref": None,
        "kind": "cli",
        "lifecycle": "active",
        "output_contract_ref": None,
        "principal_policy_ref": None,
        "record_id": "surface:cli:status",
        "record_status": "ADJUDICATION_REQUIRED",
        "record_type": "surfaces",
        "schema_version": verifier.ARTIFACT_SCHEMAS["surfaces"],
        "surface_family_id": "surface-family:cli.status",
    }


def _metadata(raw: bytes, record: dict) -> dict:
    line = verifier._canonical_json_bytes(record)
    return {
        "artifact_id": "surfaces",
        "path": "surfaces.jsonl",
        "schema_version": record["schema_version"],
        "record_type": record["record_type"],
        "record_count": 1,
        "record_id_set_sha256": verifier._set_hash([record["record_id"]]),
        "discovery_key_set_sha256": verifier._set_hash([record["discovery_key"]]),
        "record_root_sha256": verifier._record_root([line]),
        "sha256": verifier._sha256(raw),
        "byte_length": len(raw),
    }


def test_independent_verifier_does_not_import_catalog_generator() -> None:
    package_root = Path(verifier.__file__).with_name("successor_d0_verification")
    imported_modules: set[str] = set()
    for path in sorted(package_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
    cli_source = inspect.getsource(verifier._independent_cli_census)

    assert not {
        module
        for module in imported_modules
        if "successor_d0_catalog" in module or "successor_d0_generation" in module
    }
    assert "subprocess" not in cli_source
    assert "import mnemos_cli" not in cli_source


def test_hash_wire_format_is_prefixed_sha256() -> None:
    digest = verifier._sha256(b"d0")

    assert digest == ("sha256:0ad52e338662c923b15fd45a73c6e97336efccf28a7aef9449443cc6dd7415fb")
    assert verifier._canonical_json_bytes({"b": "x", "a": 1}) == b'{"a":1,"b":"x"}\n'
    assert verifier._set_hash(["b", "a", "a"]) == (
        "sha256:eb394fd4559b1d9c383f4359667a508a615b82a74e1b160fce539f86ae0842e8"
    )


def test_snapshot_rejects_non_object_revision_before_git_execution() -> None:
    findings: list[verifier.Finding] = []

    resolved = verifier._verify_snapshot(
        verifier.ROOT,
        {"commit": "HEAD", "tree": "--help"},
        findings,
    )

    assert resolved is None
    assert [finding.code for finding in findings] == ["MANIFEST_INVALID"]


def test_verifier_rejects_obsolete_single_file_generator_identity() -> None:
    findings: list[verifier.Finding] = []

    verifier._verify_generator_identity(
        {
            "module_path": "scripts/successor_d0_catalog.py",
            "module_sha256": "sha256:" + "0" * 64,
            "cli_path": "scripts/generate_successor_d0_catalog.py",
            "cli_sha256": "sha256:" + "0" * 64,
        },
        repo_root=verifier.ROOT,
        findings=findings,
    )

    assert [finding.code for finding in findings] == ["MANIFEST_INVALID"]


def test_generator_import_closure_rejects_unbound_and_dynamic_imports() -> None:
    errors = verifier_snapshot._generator_import_closure_errors(
        {
            "scripts/successor_d0_generation/__init__.py": (
                b"from .outside_identity import moved_logic\n"
                b"moved_logic = __import__('scripts.another_escape')\n"
            )
        }
    )

    assert any("unbound import" in error for error in errors)
    assert any("dynamic import" in error for error in errors)


def test_snapshot_metadata_is_a_closed_independently_enumerated_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "1" * 40
    tree = "2" * 40
    entry = verifier_snapshot._VerifierTreeBlob(
        path="a.txt",
        mode="100644",
        object_id="3" * 40,
        size=7,
    )

    def fake_git_value(_repo_root: Path, *arguments: str) -> str:
        return {
            f"{commit}^{{commit}}": commit,
            f"{commit}^{{tree}}": tree,
            "--show-object-format": "sha1",
        }[arguments[-1]]

    monkeypatch.setattr(verifier_snapshot, "_git_value", fake_git_value)
    monkeypatch.setattr(
        verifier_snapshot,
        "_verifier_tree_inventory",
        lambda _root, _commit, _format: (entry,),
    )
    exact = {
        "archive_format": "git-archive-tar+ls-tree-blob-oid-v1",
        "commit": commit,
        "file_count": 1,
        "git_object_format": "sha1",
        "requested_commit": commit,
        "total_blob_bytes": 7,
        "tree": tree,
    }
    findings: list[verifier.Finding] = []

    assert verifier_snapshot._verify_snapshot(Path("."), exact, findings) == (commit, tree)
    assert findings == []

    tampered = {**exact, "file_count": 2}
    assert verifier_snapshot._verify_snapshot(Path("."), tampered, findings) == (commit, tree)
    assert [finding.code for finding in findings] == ["SNAPSHOT_MISMATCH"]


def test_design_binding_requires_both_constitution_anchors(tmp_path: Path) -> None:
    design_path = tmp_path / "design.md"
    design_path.write_text(
        "<!-- accepted-principle:complete-function-denominator -->\n",
        encoding="utf-8",
    )

    with pytest.raises(CatalogInputError, match="anchors are missing"):
        generator_snapshot._external_exact_binding(
            binding_id="successor_d0_design",
            source_role="design",
            path=design_path,
            repo_root=tmp_path,
            required_anchor_tokens=generator_snapshot.SUCCESSOR_CONSTITUTION_ANCHORS,
        )


@pytest.mark.parametrize("use_info_attributes", [False, True])
@pytest.mark.parametrize("attribute", ["export-ignore", "export-subst"])
def test_generator_rejects_git_archive_attribute_transformations(
    tmp_path: Path,
    use_info_attributes: bool,
    attribute: str,
) -> None:
    repo, commit = _adversarial_archive_repo(
        tmp_path,
        use_info_attributes=use_info_attributes,
        attribute=attribute,
    )
    design_path = tmp_path / "design.md"
    contract_path = tmp_path / "contract.md"
    design_path.write_text(TEST_DESIGN_TEXT, encoding="utf-8")
    contract_path.write_text("# contract\n", encoding="utf-8")

    with pytest.raises(CatalogInputError, match="archive|attribute|snapshot"):
        SuccessorD0Catalog().generate(
            CatalogRequest(
                repo_root=repo,
                legacy_commit=commit,
                design_path=design_path,
                phase_contract_path=contract_path,
            )
        )


@pytest.mark.parametrize("use_info_attributes", [False, True])
@pytest.mark.parametrize("attribute", ["export-ignore", "export-subst"])
def test_verifier_rejects_git_archive_attribute_transformations(
    tmp_path: Path,
    use_info_attributes: bool,
    attribute: str,
) -> None:
    repo, commit = _adversarial_archive_repo(
        tmp_path,
        use_info_attributes=use_info_attributes,
        attribute=attribute,
    )
    destination = tmp_path / "snapshot"
    destination.mkdir()

    with pytest.raises(ValueError, match="archive|attribute|snapshot"):
        verifier._materialize_snapshot(repo, commit, destination)


def test_snapshot_blob_limit_fails_closed_in_both_implementations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, commit = _adversarial_archive_repo(tmp_path, use_info_attributes=False)
    # Remove the attribute transform from the committed tree so only the byte
    # budget is under test.
    (repo / "nested" / ".gitattributes").unlink()
    _git(repo, "add", "-u")
    _git(repo, "commit", "--quiet", "-m", "remove attributes")
    commit = _git(repo, "rev-parse", "HEAD")
    design_path = tmp_path / "design.md"
    contract_path = tmp_path / "contract.md"
    design_path.write_text(TEST_DESIGN_TEXT, encoding="utf-8")
    contract_path.write_text("# contract\n", encoding="utf-8")
    monkeypatch.setattr(generator_snapshot, "MAX_SNAPSHOT_BLOB_BYTES", 1)
    monkeypatch.setattr(verifier_snapshot, "MAX_SNAPSHOT_BLOB_BYTES", 1)

    with pytest.raises(CatalogInputError, match="blob.*limit"):
        SuccessorD0Catalog().generate(
            CatalogRequest(
                repo_root=repo,
                legacy_commit=commit,
                design_path=design_path,
                phase_contract_path=contract_path,
            )
        )
    destination = tmp_path / "limited-snapshot"
    destination.mkdir()
    with pytest.raises(ValueError, match="blob.*limit"):
        verifier._materialize_snapshot(repo, commit, destination)


def test_git_snapshot_streams_stop_before_archive_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _commit = _adversarial_archive_repo(tmp_path, use_info_attributes=False)
    (repo / "nested" / ".gitattributes").unlink()
    _git(repo, "add", "-u")
    _git(repo, "commit", "--quiet", "-m", "remove attributes")
    commit = _git(repo, "rev-parse", "HEAD")
    object_format = _git(repo, "rev-parse", "--show-object-format")
    entries = generator_snapshot._catalog_tree_inventory(repo, commit, object_format)

    generator_parent = tmp_path / "generator-bounded"
    generator_tree = generator_parent / "tree"
    generator_tree.mkdir(parents=True)
    monkeypatch.setattr(generator_snapshot, "MAX_SNAPSHOT_ARCHIVE_BYTES", 1)
    with pytest.raises(CatalogInputError, match="archive.*byte limit"):
        generator_snapshot._extract_git_archive(
            repo,
            commit,
            generator_tree,
            entries,
            object_format,
        )
    assert (generator_parent / "snapshot.tar").stat().st_size <= 1

    verifier_parent = tmp_path / "verifier-bounded"
    verifier_parent.mkdir()
    monkeypatch.setattr(verifier_snapshot, "MAX_SNAPSHOT_ARCHIVE_BYTES", 1)
    with pytest.raises(ValueError, match="archive.*byte limit"):
        verifier_snapshot._materialize_snapshot(repo, commit, verifier_parent)
    assert (verifier_parent / "snapshot.tar").stat().st_size <= 1


def test_git_tree_listing_is_bounded_before_record_accumulation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, commit = _adversarial_archive_repo(tmp_path, use_info_attributes=False)
    object_format = _git(repo, "rev-parse", "--show-object-format")
    monkeypatch.setattr(generator_snapshot, "_MAX_TREE_LISTING_BYTES", 1)
    monkeypatch.setattr(verifier_snapshot, "_MAX_TREE_LISTING_BYTES", 1)

    with pytest.raises(CatalogInputError, match="ls-tree.*byte limit"):
        generator_snapshot._catalog_tree_inventory(repo, commit, object_format)
    with pytest.raises(ValueError, match="ls-tree.*byte limit"):
        verifier_snapshot._verifier_tree_inventory(repo, commit, object_format)


def test_canonical_json_rejects_non_finite_and_invalid_unicode() -> None:
    with pytest.raises(ValueError):
        verifier._canonical_json_bytes({"value": float("nan")})
    with pytest.raises(UnicodeEncodeError):
        verifier._canonical_json_bytes({"value": "\ud800"})


def test_audit_cli_help_runs_without_pythonpath() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/audit_successor_d0_catalog.py", "--help"],
        cwd=verifier.ROOT,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "--bundle-dir" in result.stdout


def test_artifact_reader_accepts_exact_canonical_jsonl(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    record = _surface_record()
    raw = verifier._canonical_json_bytes(record)
    (bundle / "surfaces.jsonl").write_bytes(raw)
    findings: list[verifier.Finding] = []

    records = verifier._read_artifact(
        bundle,
        "surfaces",
        _metadata(raw, record),
        findings,
    )

    assert records == [record]
    assert findings == []


def test_artifact_reader_rejects_noncanonical_or_changed_bytes(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    record = _surface_record()
    canonical = verifier._canonical_json_bytes(record)
    noncanonical = (" " + canonical.decode("utf-8")).encode("utf-8")
    (bundle / "surfaces.jsonl").write_bytes(noncanonical)
    findings: list[verifier.Finding] = []

    verifier._read_artifact(
        bundle,
        "surfaces",
        _metadata(canonical, record),
        findings,
    )

    codes = {finding.code for finding in findings}
    assert "ARTIFACT_METADATA_MISMATCH" in codes
    assert "ARTIFACT_BYTES_INVALID" in codes


@pytest.mark.parametrize(
    ("limit_name", "limit_value"),
    [
        ("MAX_ARTIFACT_BYTES", 1),
        ("MAX_JSONL_LINE_BYTES", 1),
        ("MAX_JSONL_RECORDS", 0),
    ],
)
def test_artifact_resource_limits_fail_closed_with_typed_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit_value: int,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    record = _surface_record()
    raw = verifier._canonical_json_bytes(record)
    (bundle / "surfaces.jsonl").write_bytes(raw)
    monkeypatch.setattr(verifier_wire, limit_name, limit_value)
    findings: list[verifier.Finding] = []

    records = verifier._read_artifact(
        bundle,
        "surfaces",
        _metadata(raw, record),
        findings,
    )

    assert records == []
    assert [finding.code for finding in findings] == ["RESOURCE_LIMIT_EXCEEDED"]


def test_manifest_resource_limit_fails_closed_with_typed_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_bytes(verifier._canonical_json_bytes({}))
    monkeypatch.setattr(verifier_runner, "MAX_MANIFEST_BYTES", 1)

    report = verifier.verify_bundle(bundle, repo_root=verifier.ROOT)

    assert report["integrity_ok"] is False
    assert [finding["code"] for finding in report["findings"]] == ["RESOURCE_LIMIT_EXCEEDED"]


def test_independent_census_reproduces_bound_immutable_snapshot() -> None:
    with tempfile.TemporaryDirectory(prefix="mnemos-d0-verifier-test-") as temporary:
        snapshot = verifier._materialize_snapshot(
            verifier.ROOT,
            LEGACY_COMMIT,
            Path(temporary),
        )
        census = verifier._independent_static_census(snapshot)

    assert len(census["console_scripts"]) == 1
    assert len(census["cli_top"]) == 59
    assert len(census["cli_leaves"]) == 179
    assert census["cli_parameter_counts"] == {
        "boolean_action_count": 178,
        "choice_action_count": 12,
        "choice_value_count": 62,
        "optional_action_count": 347,
        "parameter_action_count": 408,
        "positional_action_count": 61,
    }
    assert census["cli_effective_facet_counts"] == {
        "effective_boolean_facet_count": 178,
        "effective_choice_facet_count": 12,
        "effective_choice_value_count": 62,
        "effective_optional_facet_count": 365,
        "effective_parameter_facet_count": 426,
        "effective_positional_facet_count": 61,
    }
    assert len(census["cli_dispatch_direct"]) == 33
    assert len(census["cli_dispatch_subcommand"]) == 23
    assert census["cli_dispatch_special"] == ["health", "mcp", "metrics"]
    assert len(census["cli_dispatch_all"]) == 59
    assert census["cli_dispatch_missing"] == []
    assert census["cli_dispatch_stale"] == []
    assert len(census["mcp_schema_tools"]) == 57
    assert census["mcp_schema_tools"] == census["mcp_registered_tools"]
    assert census["mcp_schema_tools"] == census["mcp_policy_tools"]
    assert sorted(set(census["mcp_schema_tools"]) - set(census["mcp_categorized_tools"])) == [
        "session_save"
    ]
    assert census["mcp_protocol_methods"] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/list",
    ]
    assert census["mcp_category_counts"] == {
        "advanced": 16,
        "auxiliary": 9,
        "core": 5,
        "extended": 22,
        "lifecycle": 4,
    }
    assert census["mcp_policy_counts"] == {
        "admin_runtime": 6,
        "capture_write": 5,
        "feedback_write": 10,
        "memory_read": 21,
        "memory_write": 11,
        "public_metadata": 4,
    }
    assert len(census["facade_methods"]) == 67
    assert len(census["source_ids"]) == 12
    assert len(census["daemon_intervals"]) == 38
    assert census["daemon_handler_gap"] == []
    assert census["daemon_aliases"] == ["l1_sync"]
    assert len(census["chronos_steps"]) == 26
    assert census["chronos_trigger_counts"] == {
        "condition": 1,
        "cron": 20,
        "event": 4,
        "passive": 1,
    }
    assert len(census["event_policy_persistent"]) == 16
    assert len(census["event_policy_no_persist"]) == 16
    assert len(census["event_subscription_edges"]) == 34
    assert len(census["event_subscription_topics"]) == 21
    assert census["event_subscription_wildcard_edges"] == 1
    assert census["event_subscription_unresolved"] == []
    assert len(census["health_checks"]) == 31
    assert len(census["kia_modules"]) == 5
    assert len(census["script_modules"]) == 240
    assert len(census["script_main"]) == 204
    assert len(census["script_helper"]) == 36
    assert len(census["script_reachable_helpers"]) == 34
    assert census["script_unreachable_helpers"] == [
        "scripts/__init__.py",
        "scripts/wrapper_weekly_report.py",
    ]
    assert census["script_import_time_effect_candidates"] == ["scripts/wrapper_weekly_report.py"]
    assert len(census["guarded_main_outside_scripts"]) == 33
    assert len(census["executable_files"]) == 6
    assert len(census["pytest_files"]) == 636
    assert len(census["requirement_ids"]) == 126
    assert len(census["legacy_feature_ids"]) == 39
    assert len(census["function_matrix_cli_mapped"]) == 131
    assert len(census["function_matrix_cli_unmapped"]) == 48
    assert len(census["function_matrix_mcp_mapped"]) == 44
    assert len(census["function_matrix_mcp_unmapped"]) == 13
    assert census["function_matrix_validation_ref_count"] == 218
    assert len(census["function_matrix_validation_edges"]) == 209
    assert len(census["function_matrix_validation_files"]) == 168
    assert census["function_matrix_validation_missing_files"] == [
        "tests/unit/test_storage_application.py"
    ]
    assert census["function_matrix_features_without_test_file_ref"] == [
        "agent.kit",
        "ops.init",
    ]
    assert len(census["schema_owner_paths"]) == 129
    assert verifier._set_hash(census["cli_leaves"]) == (
        "sha256:9094a2f3ce6f801f6d612b2b32a78ee0eb1c59a7e7c2df997836cd90ffb6487d"
    )
    assert verifier._set_hash(census["mcp_schema_tools"]) == (
        "sha256:f7f7524228b71032adb88598963d12db981585395acda76dda1a6ffc9e882045"
    )
    assert verifier._set_hash(census["script_modules"]) == (
        "sha256:1bb275a595651407bf0972f7c555111c2ec3cf87493c3f243903116cd53fa9fc"
    )
    assert verifier._set_hash(census["pytest_files"]) == (
        "sha256:02da0b491243d7fa8532b2a452702d45b5f1dc2a622f0111172c623670cd0740"
    )
    assert verifier._set_hash(census["schema_owner_paths"]) == (
        "sha256:51b0416b2200ba698d1395a6ff40c68d01ffe8d935739a07d570daeda7e0cbba"
    )


def test_global_identity_and_edge_direction_are_verified() -> None:
    findings: list[verifier.Finding] = []
    records = {
        "requirements": [{"record_id": "shared"}],
        "surfaces": [{"record_id": "shared"}],
        "capabilities": [{"record_id": "capability:one"}],
        "tests_oracles": [{"record_id": "oracle:one"}],
        "coverage_edges": [
            {
                "record_id": "edge:wrong-direction",
                "relation": "SURFACE_EXPOSES_CAPABILITY",
                "from_id": "capability:one",
                "to_id": "oracle:one",
            }
        ],
    }

    verifier._verify_global_identity(records, findings)
    verifier._verify_edges(records, findings)

    assert "DUPLICATE_RECORD_ID" in {finding.code for finding in findings}
    assert sum(finding.code == "INVALID_RECORD" for finding in findings) == 5


def test_v1_rejects_self_signed_decisions_and_independence() -> None:
    surface = _surface_record()
    surface["decision_ref"] = "receipt:self-signed"
    oracle = {
        "schema_version": verifier.ARTIFACT_SCHEMAS["tests_oracles"],
        "record_type": "tests_oracles",
        "record_id": "oracle:self-signed",
        "discovery_key": "oracle:self-signed",
        "record_status": "DISCOVERED",
        "evidence_refs": [],
        "kind": "pytest_file",
        "runner": {},
        "source_anchors": [],
        "fixture_refs": [],
        "mutation_operator_ids": [],
        "fault_model_ids": [],
        "asserted_observables": [],
        "evidence_schema": None,
        "population_policy": "UNKNOWN",
        "independence_class": "INDEPENDENT",
        "release_blocking": False,
        "decision_ref": None,
    }

    surface_errors = verifier._record_contract_errors("surfaces", surface)
    oracle_errors = verifier._record_contract_errors("tests_oracles", oracle)

    assert any("decision_ref" in error for error in surface_errors)
    assert any("cannot claim verified independence" in error for error in oracle_errors)


def test_v1_closure_ignores_status_owner_target_and_config_self_assertions() -> None:
    records = {
        "requirements": [],
        "surfaces": [
            {
                "record_id": "surface:facet",
                "discovery_key": "surface:facet",
                "record_status": "DISCOVERED",
                "kind": "cli_argument_facet",
            }
        ],
        "capabilities": [
            {
                "record_id": "capability:self-signed",
                "discovery_key": "capability:self-signed",
                "record_status": "ACTIVE",
                "legacy_behavior_state": "active",
                "state_contract": {"canonical_owner_id": "owner:self-signed"},
                "effect_contracts": [{"target_owner": "target:self-signed"}],
            }
        ],
        "tests_oracles": [
            {
                "record_id": "oracle:test-file",
                "discovery_key": "oracle:test-file",
                "record_status": "LINKED_DECLARATION",
                "kind": "pytest_file",
                "source_path": "tests/unit/test_self_signed.py",
                "source_anchors": [],
                "independence_class": "INDEPENDENT",
                "release_blocking": False,
            }
        ],
        "coverage_edges": [],
    }

    counts = verifier._independent_closure_counts(
        records,
        source_bindings=[],
        config_snapshot={"mode": "EXACT_FILE", "sha256": "sha256:self-signed"},
        generator_findings=[],
        independent_inventory_diff=0,
    )

    assert counts["freeze_evaluator_unimplemented"] == 1
    assert counts["capability_without_independent_test_or_oracle"] == 1
    assert counts["canonical_owner_unknown"] == 1
    assert counts["effect_target_unknown"] == 1
    assert counts["test_file_without_disposition"] == 1
    assert counts["parameter_mode_unclassified"] == 1
    assert counts["independent_inventory_pending_family"] == 5
    assert counts["config_applicability_attestation_gap"] == 1
    assert counts["constitution_requirement_missing"] == 2
    assert counts["constitution_approval_missing"] == 1
    assert counts["unresolved_adjudication"] == 3


def test_generator_is_byte_deterministic_and_interoperates_with_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design_path = tmp_path / "design.md"
    contract_path = tmp_path / "phase-contract.md"
    design_path.write_text(TEST_DESIGN_TEXT, encoding="utf-8")
    contract_path.write_text("# test phase contract\n", encoding="utf-8")
    request = CatalogRequest(
        repo_root=verifier.ROOT,
        legacy_commit=LEGACY_COMMIT,
        design_path=design_path,
        phase_contract_path=contract_path,
        config_snapshot=None,
    )
    generator = SuccessorD0Catalog()

    first = generator.generate(request)
    second = generator.generate(request)

    assert first.artifacts == second.artifacts
    generator_identity = first.manifest["generator_identity"]
    generator_paths = [row["path"] for row in generator_identity["implementation_files"]]
    assert generator_identity["code_identity_version"] == "exact-file-set-v1"
    assert generator_paths == sorted(generator_paths)
    assert generator_paths == EXPECTED_GENERATOR_IDENTITY_PATHS
    assert generator_identity["implementation_root_sha256"] == verifier._sha256(
        verifier._canonical_json_bytes(
            [
                [row["path"], row["sha256"], row["byte_length"]]
                for row in generator_identity["implementation_files"]
            ]
        )
    )
    assert [item["record_count"] for item in first.manifest["artifacts"]] == [
        128,
        1390,
        39,
        2100,
        426,
    ]
    manifest_text = json.dumps(first.manifest, ensure_ascii=False)
    assert str(tmp_path) not in manifest_text
    assert {
        item["binding_id"]: item["locator"]
        for item in first.manifest["source_bindings"]
        if item["binding_kind"] == "external_exact_file"
    } == {
        "phase0_7_global_engineering_contract": ("external:phase0_7_global_engineering_contract"),
        "successor_d0_design": "external:successor_d0_design",
    }
    bundle_dir = tmp_path / "bundle"
    generator.write(first, bundle_dir)
    report = verifier.verify_bundle(
        bundle_dir,
        repo_root=verifier.ROOT,
        external_bindings={
            "successor_d0_design": design_path,
            "phase0_7_global_engineering_contract": contract_path,
        },
    )

    codes = {finding["code"] for finding in report["findings"]}
    verifier_identity = report["verifier_identity"]
    verifier_paths = [row["path"] for row in verifier_identity["implementation_files"]]
    assert verifier_identity["code_identity_version"] == "exact-file-set-v1"
    assert verifier_paths == sorted(verifier_paths)
    assert verifier_paths == EXPECTED_VERIFIER_IDENTITY_PATHS
    assert verifier_identity["implementation_root_sha256"] == verifier._sha256(
        verifier._canonical_json_bytes(
            [
                [row["path"], row["sha256"], row["byte_length"]]
                for row in verifier_identity["implementation_files"]
            ]
        )
    )
    assert report["verification_status"] == "BLOCKED"
    assert report["ok"] is False
    assert report["freeze_ready"] is False
    assert report["freeze_protocol_implemented"] is False
    assert report["independent_inventory"]["complete"] is False
    assert report["independent_inventory"]["pending_families"] == sorted(
        verifier.REQUIRED_INDEPENDENT_INVENTORY_FAMILIES
    )
    assert (
        report["verified_closure_counts"]
        | {
            "freeze_evaluator_unimplemented": 1,
            "test_file_without_disposition": 350,
            "script_parameter_contract_unknown": 204,
            "independent_inventory_diff": 1,
            "independent_inventory_pending_family": 5,
            "config_applicability_attestation_gap": 1,
            "constitution_requirement_missing": 0,
            "constitution_approval_missing": 1,
        }
        == report["verified_closure_counts"]
    )
    assert report["independent_inventory"]["diffs"] == {
        "schema_reverse_paths": {
            "missing": [
                "core/sync_framework/capture_queue.py",
                "scripts/reconcile_observation_provenance_edges.py",
                "scripts/reconcile_raw_index_paths.py",
            ],
            "extra": [],
        }
    }
    assert (
        not {
            "ARTIFACT_BYTES_INVALID",
            "ARTIFACT_METADATA_MISMATCH",
            "BINDING_MISSING",
            "CLOSURE_MISMATCH",
            "EDGE_ENDPOINT_MISSING",
            "EVIDENCE_REF_INVALID",
            "INVALID_RECORD",
            "MANIFEST_INVALID",
            "SNAPSHOT_MISMATCH",
        }
        & codes
    )

    parsed_records = {
        artifact_id: [
            json.loads(line) for line in first.artifacts[f"{artifact_id}.jsonl"].splitlines()
        ]
        for artifact_id in verifier.ARTIFACT_ORDER
    }
    tampered_records = copy.deepcopy(parsed_records)
    tampered_edge = next(
        edge
        for edge in tampered_records["coverage_edges"]
        if edge["relation"] == "SURFACE_EXPOSES_CAPABILITY"
    )
    existing_contracts = {
        (
            edge["from_id"],
            edge["relation"],
            edge["to_id"],
            edge["facet"],
            edge["assertion_authority"],
        )
        for edge in tampered_records["coverage_edges"]
    }
    replacement_surface = next(
        surface["record_id"]
        for surface in tampered_records["surfaces"]
        if (
            surface["record_id"],
            tampered_edge["relation"],
            tampered_edge["to_id"],
            tampered_edge["facet"],
            tampered_edge["assertion_authority"],
        )
        not in existing_contracts
    )
    tampered_edge["from_id"] = replacement_surface
    edge_identity = {
        "from_id": tampered_edge["from_id"],
        "relation": tampered_edge["relation"],
        "to_id": tampered_edge["to_id"],
        "facet": tampered_edge["facet"],
    }
    edge_identity_json = json.dumps(
        edge_identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    tampered_edge["record_id"] = (
        "edge:sha256:" + hashlib.sha256(edge_identity_json.encode("utf-8")).hexdigest()
    )
    tampered_edge["discovery_key"] = (
        f"edge:{tampered_edge['relation']}:{tampered_edge['from_id']}:"
        f"{tampered_edge['to_id']}:{tampered_edge['facet']}"
    )
    structural_findings: list[verifier.Finding] = []
    verifier._verify_edges(tampered_records, structural_findings)
    assert structural_findings == []

    wrong_metrics = copy.deepcopy(first.manifest["inventory_metrics"])
    wrong_metrics["main_cli"]["leaf_count"] += 1
    with tempfile.TemporaryDirectory(prefix="mnemos-d0-edge-attack-") as temporary:
        snapshot = verifier._materialize_snapshot(
            verifier.ROOT,
            LEGACY_COMMIT,
            Path(temporary),
        )
        census_findings: list[verifier.Finding] = []
        census = verifier._verify_independent_census(
            snapshot,
            tampered_records,
            census_findings,
            inventory_metrics=wrong_metrics,
        )
    census_codes = {finding.code for finding in census_findings}
    assert "EDGE_INVENTORY_DIFF" in census_codes
    assert "INDEPENDENT_INVENTORY_DIFF" in census_codes
    assert census["diffs"]["coverage_edge_multiset"]["missing"]
    assert census["diffs"]["coverage_edge_multiset"]["extra"]
    assert "manifest_inventory_metrics" in census["diffs"]

    tampered_manifest: dict = copy.deepcopy(dict(first.manifest))
    tampered_manifest["decorative_self_claim"] = "verified"
    tampered_manifest["canonicalization"]["json"] = "writer-defined"
    tampered_manifest["closure"]["schema_version"] = "writer-defined"
    tampered_manifest["closure"]["counts"]["test_file_denominator"] += 1
    tampered_manifest["closure"]["unknown_count_owner"] = "writer"
    tampered_manifest["artifacts"][0]["decorative_self_claim"] = True
    tampered_artifacts = dict(first.artifacts)
    tampered_artifacts["manifest.json"] = verifier._canonical_json_bytes(tampered_manifest)
    tampered_bundle = type(first)(manifest=tampered_manifest, artifacts=tampered_artifacts)
    tampered_dir = tmp_path / "tampered-bundle"
    generator.write(tampered_bundle, tampered_dir)
    monkeypatch.setattr(verifier_runner, "_verify_snapshot", lambda *_args, **_kwargs: None)
    tampered_report = verifier.verify_bundle(
        tampered_dir,
        repo_root=verifier.ROOT,
        external_bindings={
            "successor_d0_design": design_path,
            "phase0_7_global_engineering_contract": contract_path,
        },
    )
    tampered_codes = {finding["code"] for finding in tampered_report["findings"]}
    assert "MANIFEST_INVALID" in tampered_codes
    assert "CLOSURE_MISMATCH" in tampered_codes
    assert tampered_report["legacy_snapshot"] == {}


def test_generator_cli_preview_does_not_write(tmp_path: Path) -> None:
    design_path = tmp_path / "design.md"
    contract_path = tmp_path / "phase-contract.md"
    output_dir = tmp_path / "must-not-exist"
    design_path.write_text(TEST_DESIGN_TEXT, encoding="utf-8")
    contract_path.write_text("# test phase contract\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "scripts/generate_successor_d0_catalog.py",
            "--legacy-commit",
            LEGACY_COMMIT,
            "--design-path",
            str(design_path),
            "--phase-contract-path",
            str(contract_path),
            "--output-dir",
            str(output_dir),
            "--json",
        ],
        cwd=verifier.ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert report["mode"] == "preview"
    assert report["bundle_status"] == "BLOCKED"
    assert report["written_paths"] == []
    assert not output_dir.exists()


def test_audit_cli_is_fail_closed_without_strict_flag(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "scripts/audit_successor_d0_catalog.py",
            "--bundle-dir",
            str(tmp_path / "missing-bundle"),
            "--json",
        ],
        cwd=verifier.ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["verification_status"] == "BLOCKED"
