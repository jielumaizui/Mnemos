"""Trustworthy per-gate execution and executable mutation contracts."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from core.file_ops import sha256_file
from core.ops.hermetic_run import HermeticRunEnvironment
from core.utils import atomic_write_text, load_json_value, read_text_value

SCHEMA_VERSION = "mnemos.gate_execution.v1"
MUTATION_SCHEMA_VERSION = "mnemos.executable_mutation_result.v1"
SEMANTIC_MUTATION_OPERATORS = frozenset(
    {
        "delete_required_gate",
        "drop_manifest_item",
    }
)
NON_SEMANTIC_MUTATION_OPERATORS = frozenset(
    {
        "name_only",
        "empty_body",
        "reverse_assertion",
    }
)
PYTEST_PLUGIN_GUARDIAN_SOURCE = """\
import pytest

_configured = False
_known_plugin_ids = set()


def pytest_sessionstart(session):
    global _configured, _known_plugin_ids
    _known_plugin_ids = {id(plugin) for plugin in session.config.pluginmanager.get_plugins()}
    _configured = True


def pytest_plugin_registered(plugin, manager):
    if _configured and id(plugin) not in _known_plugin_ids:
        if (
            manager.get_name(plugin) == "funcmanage"
            and type(plugin).__module__ == "_pytest.fixtures"
            and type(plugin).__qualname__ == "FixtureManager"
        ):
            _known_plugin_ids.add(id(plugin))
            return
        raise pytest.UsageError(
            "explicit pytest plugins are forbidden for exact gates: "
            + str(manager.get_name(plugin))
            + ":"
            + type(plugin).__module__
            + "."
            + type(plugin).__qualname__
        )
"""


def _sha256(path: Path) -> str:
    return str(sha256_file(path))


def _sandboxed_argv(argv: Sequence[str], *, allow_root: Path) -> tuple[str, ...]:
    """Return an OS write-denied argv on macOS, fail closed elsewhere."""

    if platform.system() != "Darwin":
        raise RuntimeError("strict per-gate OS write denial is unsupported on this platform")
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        raise RuntimeError("sandbox-exec is required for strict per-gate execution")
    escaped = str(allow_root).replace("\\", "\\\\").replace('"', '\\"')
    profile = (
        "(version 1)(allow default)(deny file-write*)"
        '(allow file-write* (literal "/dev/null"))'
        f'(allow file-write* (subpath "{escaped}"))'
    )
    return (sandbox_exec, "-p", profile, *argv)


@dataclass(frozen=True)
class GateRunnerSelector:
    """Describe one exact, repository-owned gate invocation."""

    runner_kind: str
    entrypoint: str
    argv: tuple[str, ...] = ()
    node_ids: tuple[str, ...] = ()

    @staticmethod
    def _validate_pytest_node(node_id: str, *, repo_root: Path) -> list[str]:
        errors: list[str] = []
        if any(character.isspace() for character in node_id) or node_id.startswith("-"):
            return ["pytest node_ids must not contain argv or whitespace"]
        parts = node_id.split("::")
        if len(parts) not in {2, 3} or any(not part for part in parts):
            return ["pytest node_ids must select one exact test function or method"]
        relative, *symbols = parts
        if not re.fullmatch(r"tests/(?:[A-Za-z0-9_.-]+/)*test_[A-Za-z0-9_.-]+\.py", relative):
            return ["pytest node_ids must use an exact repo test file"]
        path = (repo_root / relative).resolve(strict=False)
        if not path.is_file() or repo_root.resolve() not in path.parents:
            return ["pytest node_ids must reference an existing repo test file"]
        symbol_names = [symbol.split("[", 1)[0] for symbol in symbols]
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol) for symbol in symbol_names):
            return ["pytest node_ids must use exact Python test symbols"]
        try:
            tree = ast.parse(read_text_value(path), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            return [f"pytest node file cannot be inspected: {exc}"]
        for node in tree.body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "pytest_plugins" for target in targets
            ):
                errors.append("pytest node file must not declare pytest_plugins")
        if len(symbol_names) == 1:
            exists = any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == symbol_names[0]
                for node in tree.body
            )
        else:
            class_name, method_name = symbol_names
            exists = any(
                isinstance(node, ast.ClassDef)
                and node.name == class_name
                and any(
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == method_name
                    for child in node.body
                )
                for node in tree.body
            )
        if not exists:
            errors.append("pytest node_ids must reference an existing exact test symbol")
        return errors

    def validate(self, *, repo_root: Path) -> list[str]:
        errors: list[str] = []
        if self.runner_kind == "python":
            if self.node_ids:
                errors.append("python runner node_ids must be empty")
            path = (repo_root / self.entrypoint).resolve(strict=False)
            if not path.is_file() or repo_root.resolve() not in path.parents:
                errors.append("python runner entrypoint must be a repo-owned file")
        elif self.runner_kind == "pytest":
            if self.entrypoint:
                errors.append("pytest runner entrypoint must be empty")
            if self.argv:
                errors.append(
                    "pytest runner argv must be empty; node_ids are the complete selector"
                )
            if not self.node_ids or len(self.node_ids) != len(set(self.node_ids)):
                errors.append("pytest runner requires unique exact node_ids")
            for node_id in self.node_ids:
                errors.extend(self._validate_pytest_node(node_id, repo_root=repo_root))
        else:
            errors.append("unsupported runner_kind")
        return errors

    def command(self, *, repo_root: Path) -> tuple[str, ...]:
        errors = self.validate(repo_root=repo_root)
        if errors:
            raise ValueError("; ".join(errors))
        if self.runner_kind == "python":
            return (sys.executable, str(repo_root / self.entrypoint), *self.argv)
        return (
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:benchmark",
            "--noconftest",
            "-o",
            "xfail_strict=true",
            *self.node_ids,
            *self.argv,
        )


@dataclass(frozen=True)
class GateExecutionResult:
    """Capture hermetic execution evidence for one gate invocation."""

    gate_id: str
    selector: Mapping[str, object]
    sandbox_root: str
    environment_hash: str
    returncode: int
    stdout_path: str
    stderr_path: str
    stdout_sha256: str
    stderr_sha256: str
    formal_state_diff: tuple[str, ...]
    outside_write_count: int
    os_write_guard: str
    semantic_failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.outside_write_count == 0 and not self.semantic_failures


class GateExecutionEnvironment:
    """Create one absent, unique HermeticRunEnvironment for exactly one gate."""

    def __init__(
        self,
        *,
        base_root: Path,
        repo_root: Path,
        gate_id: str,
        base_environment: Mapping[str, str] | None = None,
    ):
        if not gate_id or "/" in gate_id or ".." in gate_id:
            raise ValueError("gate_id must be a safe non-empty identifier")
        unique = f"{gate_id}-{uuid.uuid4().hex}"
        self.repo_root = repo_root.resolve()
        self.run = HermeticRunEnvironment.create(
            base_root.resolve(strict=False) / unique,
            profile="isolated",
            base_environment=base_environment,
        )
        self.gate_id = gate_id

    def execute(self, selector: GateRunnerSelector) -> GateExecutionResult:
        artifacts = Path(self.run.environment["MNEMOS_RUN_ARTIFACTS_DIR"])
        command = selector.command(repo_root=self.repo_root)
        junit_path = artifacts / "pytest-junit.xml"
        if selector.runner_kind == "pytest":
            pytest_config_path = artifacts / "pytest.ini"
            pytest_guardian_path = artifacts / "mnemos_gate_plugin_guardian.py"
            atomic_write_text(  # trusted-scan: artifact owner=ops target=gate_pytest_config expires=never sandbox-only
                pytest_config_path,
                "[pytest]\naddopts =\n",
            )
            atomic_write_text(  # trusted-scan: artifact owner=ops target=gate_guardian expires=never sandbox-only
                pytest_guardian_path,
                PYTEST_PLUGIN_GUARDIAN_SOURCE,
            )
            command = (
                *command,
                "-p",
                "mnemos_gate_plugin_guardian",
                "-c",
                str(pytest_config_path),
                "-rX",
                "--junitxml",
                str(junit_path),
            )
        guarded = _sandboxed_argv(command, allow_root=self.run.root)
        environment = dict(self.run.environment)
        if selector.runner_kind == "pytest":
            environment["PYTEST_ADDOPTS"] = ""
            environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            existing_pythonpath = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = str(artifacts) + (
                os.pathsep + existing_pythonpath if existing_pythonpath else ""
            )
        completed = subprocess.run(
            guarded,
            cwd=self.repo_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        semantic_failures: list[str] = []
        if selector.runner_kind == "pytest" and completed.returncode == 0:
            try:
                root = ET.parse(junit_path).getroot()
                suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
                tests = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
                skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
                if tests <= 0:
                    semantic_failures.append("pytest selector executed zero tests")
                if skipped:
                    semantic_failures.append(
                        f"pytest selector produced {skipped} skipped or xfailed tests"
                    )
                if any(line.startswith("XPASS ") for line in stdout.splitlines()):
                    semantic_failures.append("pytest selector produced XPASS")
            except (OSError, ET.ParseError, ValueError) as exc:
                semantic_failures.append(f"pytest semantic artifact invalid: {exc}")
        stdout_path = artifacts / "stdout.txt"
        stderr_path = artifacts / "stderr.txt"
        atomic_write_text(  # trusted-scan: artifact owner=ops target=gate_stdout expires=never sandbox-only
            stdout_path,
            stdout,
        )
        atomic_write_text(  # trusted-scan: artifact owner=ops target=gate_stderr expires=never sandbox-only
            stderr_path,
            stderr,
        )
        formal_diff = tuple(self.run.finalize())
        return GateExecutionResult(
            gate_id=self.gate_id,
            selector=asdict(selector),
            sandbox_root=str(self.run.root),
            environment_hash=self.run.environment_hash,
            returncode=completed.returncode,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
            stderr_sha256=hashlib.sha256(stderr.encode()).hexdigest(),
            formal_state_diff=formal_diff,
            outside_write_count=len(formal_diff),
            os_write_guard="sandbox-exec-v1",
            semantic_failures=tuple(semantic_failures),
        )


@dataclass(frozen=True)
class ExecutableMutationSpec:
    """Bind a structural mutation and its oracle to exact artifact hashes."""

    mutation_id: str
    operator: str
    baseline_path: str
    baseline_sha256: str
    candidate_path: str
    candidate_sha256: str
    collection_path: tuple[str, ...]
    identity_field: str
    removed_identity: str
    selector: GateRunnerSelector
    selector_sha256: str
    killed_returncodes: tuple[int, ...]

    @staticmethod
    def _artifact(path: Path) -> object:
        return load_json_value(path)

    def _validate_structural_mutation(
        self,
        *,
        baseline: Path,
        candidate: Path,
    ) -> list[str]:
        if self.operator not in {"delete_required_gate", "drop_manifest_item"}:
            return []
        if not self.collection_path or not self.identity_field or not self.removed_identity:
            return ["structural mutation requires collection path, identity field and removed id"]
        try:
            baseline_payload = self._artifact(baseline)
            candidate_payload = self._artifact(candidate)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return [f"structural mutation artifact is invalid JSON: {exc}"]
        baseline_collection: object = baseline_payload
        candidate_collection: object = candidate_payload
        try:
            for key in self.collection_path:
                if not isinstance(baseline_collection, dict) or not isinstance(
                    candidate_collection, dict
                ):
                    raise KeyError(key)
                baseline_collection = baseline_collection[key]
                candidate_collection = candidate_collection[key]
        except KeyError:
            return ["structural mutation collection path is missing"]
        if not isinstance(baseline_collection, list) or not isinstance(candidate_collection, list):
            return ["structural mutation collection must be a list"]
        matches = [
            item
            for item in baseline_collection
            if isinstance(item, dict) and item.get(self.identity_field) == self.removed_identity
        ]
        if len(matches) != 1:
            return ["structural mutation must identify exactly one baseline item"]
        expected_collection = [
            item
            for item in baseline_collection
            if not (
                isinstance(item, dict) and item.get(self.identity_field) == self.removed_identity
            )
        ]
        expected_candidate = copy.deepcopy(baseline_payload)
        cursor = expected_candidate
        for key in self.collection_path[:-1]:
            if not isinstance(cursor, dict):
                return ["structural mutation collection path is missing"]
            cursor = cursor[key]
        if not isinstance(cursor, dict):
            return ["structural mutation collection path is missing"]
        cursor[self.collection_path[-1]] = expected_collection
        if candidate_payload != expected_candidate:
            return ["candidate must differ only by the declared semantic removal"]
        return []

    def validate(self, *, repo_root: Path) -> list[str]:
        errors = self.selector.validate(repo_root=repo_root)
        if self.operator in NON_SEMANTIC_MUTATION_OPERATORS:
            errors.append("non-semantic mutation operator cannot receive kill credit")
        elif self.operator not in SEMANTIC_MUTATION_OPERATORS:
            errors.append("unsupported semantic mutation operator")
        baseline = (repo_root / self.baseline_path).resolve(strict=False)
        candidate = (repo_root / self.candidate_path).resolve(strict=False)
        if not baseline.is_file() or repo_root.resolve() not in baseline.parents:
            errors.append("baseline artifact must be a repo-owned file")
        elif _sha256(baseline) != self.baseline_sha256:
            errors.append("baseline artifact hash mismatch")
        if not candidate.is_file() or repo_root.resolve() not in candidate.parents:
            errors.append("candidate artifact must be a repo-owned file")
        elif _sha256(candidate) != self.candidate_sha256:
            errors.append("candidate artifact hash mismatch")
        if self.selector.runner_kind != "python" or self.selector.argv:
            errors.append("mutation selector must be a Python oracle with empty base argv")
        selector_path = (repo_root / self.selector.entrypoint).resolve(strict=False)
        if selector_path.is_file() and _sha256(selector_path) != self.selector_sha256:
            errors.append("mutation selector hash mismatch")
        if not self.killed_returncodes or 0 in self.killed_returncodes:
            errors.append("killed_returncodes must be non-empty and exclude zero")
        if not errors:
            errors.extend(
                self._validate_structural_mutation(
                    baseline=baseline,
                    candidate=candidate,
                )
            )
        return errors


def execute_mutation(
    spec: ExecutableMutationSpec,
    *,
    base_root: Path,
    repo_root: Path,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Execute the candidate; never infer a kill from names or source strings."""

    errors = spec.validate(repo_root=repo_root)
    if errors:
        return {
            "schema_version": MUTATION_SCHEMA_VERSION,
            "ok": False,
            "mutation_id": spec.mutation_id,
            "operator": spec.operator,
            "errors": errors,
            "executed": False,
            "killed": False,
        }
    baseline_environment = GateExecutionEnvironment(
        base_root=base_root,
        repo_root=repo_root,
        gate_id=f"mutation-input-{spec.mutation_id}",
        base_environment=base_environment,
    )
    baseline_input = (
        Path(baseline_environment.run.environment["MNEMOS_RUN_ARTIFACTS_DIR"])
        / "mutation-input.json"
    )
    atomic_write_text(  # trusted-scan: artifact owner=ops target=mutation_input expires=never sandbox-only
        baseline_input,
        read_text_value(repo_root / spec.baseline_path),
    )
    baseline_result = baseline_environment.execute(
        replace(spec.selector, argv=(str(baseline_input),))
    )
    candidate_environment = GateExecutionEnvironment(
        base_root=base_root,
        repo_root=repo_root,
        gate_id=f"mutation-input-{spec.mutation_id}",
        base_environment=base_environment,
    )
    candidate_input = (
        Path(candidate_environment.run.environment["MNEMOS_RUN_ARTIFACTS_DIR"])
        / "mutation-input.json"
    )
    atomic_write_text(  # trusted-scan: artifact owner=ops target=mutation_input expires=never sandbox-only
        candidate_input,
        read_text_value(repo_root / spec.candidate_path),
    )
    candidate_result = candidate_environment.execute(
        replace(spec.selector, argv=(str(candidate_input),))
    )
    killed = (
        baseline_result.ok
        and candidate_result.returncode in spec.killed_returncodes
        and candidate_result.outside_write_count == 0
    )
    return {
        "schema_version": MUTATION_SCHEMA_VERSION,
        "ok": killed,
        "mutation_id": spec.mutation_id,
        "operator": spec.operator,
        "selector_sha256": spec.selector_sha256,
        "baseline_path": spec.baseline_path,
        "baseline_sha256": spec.baseline_sha256,
        "candidate_path": spec.candidate_path,
        "candidate_sha256": spec.candidate_sha256,
        "semantic_transformation": {
            "collection_path": list(spec.collection_path),
            "identity_field": spec.identity_field,
            "removed_identity": spec.removed_identity,
        },
        "executed": True,
        "killed": killed,
        "baseline_execution": asdict(baseline_result),
        "candidate_execution": asdict(candidate_result),
    }
