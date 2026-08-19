#!/usr/bin/env python3
"""Execute Phase 1 exact oracles against isolated historical source mutations."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Iterator

from core.ops.hermetic_run import (
    HermeticRunEnvironment,
    verify_environment_manifest,
)
from core.ops.durable_io import (
    DurableIOError,
    regular_file_sha256,
    secure_atomic_write_bytes,
)
from core.ops.durable_io import read_native_bytes

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_phase0_governance_contracts import (
    PHASE1_BASELINE_COMMITS,
    PHASE1_ROOT_REQUIREMENT_SPECS,
    phase1_execution_denominator_summary,
)

SCHEMA_VERSION = "mnemos.phase1_historical_defect_execution_evidence.v4"
_CREDIT_OUTCOMES = ("passed", "failed")
_NONCREDIT_OUTCOMES = ("error", "skipped", "xfail", "xpass")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _root_id(spec: dict[str, Any]) -> str:
    return "COG-" + str(spec["requirement_id"]).rsplit("COG-", 1)[1]


def _phase1_specs() -> tuple[dict[str, Any], ...]:
    specs = tuple(PHASE1_ROOT_REQUIREMENT_SPECS)
    roots = {_root_id(spec) for spec in specs}
    if roots != set(PHASE1_BASELINE_COMMITS):
        raise RuntimeError("Phase 1 baseline Root mapping is incomplete")
    if len({str(spec["requirement_id"]) for spec in specs}) != len(specs):
        raise RuntimeError("Phase 1 requirement identifiers are not unique")
    return specs


def _extract_commit(commit: str, destination: Path) -> None:
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", commit],
        cwd=ROOT,
        stdout=subprocess.PIPE,
    )
    if archive.stdout is None:
        raise RuntimeError("git archive stdout is unavailable")
    try:
        with tarfile.open(fileobj=archive.stdout, mode="r|") as bundle:
            bundle.extractall(destination, filter="data")
    finally:
        archive.stdout.close()
    if archive.wait() != 0:
        raise RuntimeError(f"could not materialize commit {commit}")


def _git_paths(*args: str) -> tuple[str, ...]:
    output = subprocess.check_output(
        ["git", *args, "-z"],
        cwd=ROOT,
    )
    return tuple(part.decode("utf-8", errors="strict") for part in output.split(b"\0") if part)


def _current_source_paths() -> tuple[str, ...]:
    tracked = set(_git_paths("ls-files"))
    untracked = set(_git_paths("ls-files", "--others", "--exclude-standard"))
    return tuple(sorted(tracked | untracked))


def _materialize_current_tree(
    destination: Path,
    source_paths: tuple[str, ...],
) -> None:
    """Materialize HEAD plus every non-ignored current worktree byte."""
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()
    _extract_commit(head, destination)
    head_paths = set(_git_paths("ls-tree", "-r", "--name-only", head))
    current_paths = set(source_paths)
    for relative in sorted(head_paths - current_paths):
        target = destination / relative
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    for relative in source_paths:
        source = ROOT / relative
        target = destination / relative
        try:
            metadata = source.lstat()
        except FileNotFoundError:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if stat.S_ISLNK(metadata.st_mode):
            target.unlink(missing_ok=True)
            target.symlink_to(os.readlink(source))
        elif stat.S_ISREG(metadata.st_mode):
            target.write_bytes(read_native_bytes(source))
            target.chmod(stat.S_IMODE(metadata.st_mode))
        else:
            raise RuntimeError(f"current source path is not a regular file: {relative}")


def _initialize_candidate_repository(destination: Path) -> None:
    """Give repository-aware oracles an isolated Git identity.

    The frozen candidate has its own index, refs, and synthetic HEAD, but
    governance oracles must still resolve the reviewed baseline commits named
    by the Phase 1 contract.  A read-only object alternate exposes those
    immutable objects without sharing working-tree or index state.
    """
    subprocess.run(
        ["git", "init", "-q"],
        cwd=destination,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    common_dir_value = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=ROOT,
        text=True,
    ).strip()
    common_dir = Path(common_dir_value)
    if not common_dir.is_absolute():
        common_dir = ROOT / common_dir
    source_object_dir = (common_dir / "objects").resolve()
    if not source_object_dir.is_dir():
        raise RuntimeError("source Git object directory is unavailable")
    alternates_path = destination / ".git" / "objects" / "info" / "alternates"
    alternates_path.parent.mkdir(parents=True, exist_ok=True)
    alternates_path.write_text(
        str(source_object_dir) + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "--all"],
        cwd=destination,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Mnemos Phase1 Evidence",
            "-c",
            "user.email=phase1-evidence@localhost",
            "commit",
            "-q",
            "-m",
            "frozen phase1 candidate",
        ],
        cwd=destination,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def _path_identity(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"path": relative, "kind": "missing"}
    if stat.S_ISLNK(metadata.st_mode):
        return {
            "path": relative,
            "kind": "symlink",
            "target": os.readlink(path),
        }
    if stat.S_ISREG(metadata.st_mode):
        return {
            "path": relative,
            "kind": "file",
            "sha256": regular_file_sha256(path),
        }
    return {"path": relative, "kind": "unsafe"}


def _snapshot(
    root: Path,
    paths: tuple[str, ...],
) -> dict[str, Any]:
    entries = [_path_identity(root, relative) for relative in paths]
    return {
        "path_count": len(entries),
        "sha256": _sha256_bytes(_canonical_bytes(entries)),
    }


def _execution_snapshot_paths(
    source_paths: tuple[str, ...],
    specs: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    exact_paths = {str(node).split("::", 1)[0] for spec in specs for node in spec["node_ids"]}
    exact_paths.update(str(path) for spec in specs for path in spec["candidate_paths"])
    exact_paths.update(
        {
            "scripts/generate_phase0_governance_contracts.py",
            "scripts/generate_phase1_baseline_execution_evidence.py",
            "scripts/refresh_phase1_deep_audit_governance.py",
        }
    )
    code_prefixes = ("core/", "daemon/", "integrations/", "scripts/", "tests/")
    for relative in source_paths:
        if relative.startswith(code_prefixes):
            exact_paths.add(relative)
        elif "/" not in relative and (
            relative.endswith((".py", ".toml", ".json", ".yaml", ".yml"))
        ):
            exact_paths.add(relative)
    return tuple(sorted(exact_paths))


def _git_blob(commit: str, relative: str) -> bytes | None:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{relative}"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode:
        return None
    return subprocess.check_output(
        ["git", "show", f"{commit}:{relative}"],
        cwd=ROOT,
    )


def _current_bytes(path: Path) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        return ("symlink:" + os.readlink(path)).encode("utf-8")
    if stat.S_ISREG(metadata.st_mode):
        return read_native_bytes(path)
    return None


def _replace_with_blob(path: Path, content: bytes | None) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    if content is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _apply_historical_mutation(
    candidate_root: Path,
    *,
    baseline_commit: str,
    candidate_paths: tuple[str, ...],
) -> tuple[list[dict[str, Any]], dict[str, bytes | None]]:
    changes: list[dict[str, Any]] = []
    originals: dict[str, bytes | None] = {}
    for relative in candidate_paths:
        target = candidate_root / relative
        candidate = _current_bytes(target)
        historical = _git_blob(baseline_commit, relative)
        originals[relative] = candidate
        if candidate == historical:
            continue
        changes.append(
            {
                "path": relative,
                "operation": "delete" if historical is None else "replace",
                "candidate_sha256": (_sha256_bytes(candidate) if candidate is not None else None),
                "historical_sha256": (
                    _sha256_bytes(historical) if historical is not None else None
                ),
            }
        )
        _replace_with_blob(target, historical)
    if not changes:
        raise RuntimeError("declared historical mutation has no structural delta")
    return changes, originals


def _apply_source_replacement_mutation(
    candidate_root: Path,
    replacement: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, bytes | None]]:
    relative = str(replacement["path"])
    path = candidate_root / relative
    candidate = read_native_bytes(path)
    old = str(replacement["old"]).encode("utf-8")
    new = str(replacement["new"]).encode("utf-8")
    if candidate.count(old) != 1 or old == new:
        raise RuntimeError(f"explicit mutation {replacement['operator_id']} lacks one exact seam")
    mutated = candidate.replace(old, new, 1)
    path.write_bytes(mutated)
    return (
        [
            {
                "path": relative,
                "operation": "replace_exact_text",
                "candidate_sha256": _sha256_bytes(candidate),
                "mutated_sha256": _sha256_bytes(mutated),
                "replacement_contract_sha256": _sha256_bytes(
                    _canonical_bytes(
                        {
                            "old": replacement["old"],
                            "new": replacement["new"],
                        }
                    )
                ),
            }
        ],
        {relative: candidate},
    )


def _restore_candidate(
    candidate_root: Path,
    originals: dict[str, bytes | None],
) -> None:
    for relative, content in originals.items():
        _replace_with_blob(candidate_root / relative, content)


def _parse_outcomes(output: str) -> dict[str, list[str]]:
    outcomes: dict[str, list[str]] = {
        "passed": [],
        "failed": [],
        "error": [],
        "skipped": [],
        "xfail": [],
        "xpass": [],
    }
    statuses = tuple(status.upper() for status in outcomes)
    for raw_line in output.splitlines():
        line = raw_line.strip()
        for status in statuses:
            marker = f" {status}"
            if marker not in line:
                continue
            node = line.split(marker, 1)[0].strip()
            if "::" in node:
                outcomes[status.lower()].append(node)
            break
    return {key: sorted(set(values)) for key, values in outcomes.items()}


def _run_nodes(
    cwd: Path,
    node_ids: tuple[str, ...],
    *,
    execution_id: str,
    snapshot_hash: str,
) -> dict[str, Any]:
    runtime = ROOT / ".venv" / "bin" / "python"
    safe_execution_id = "".join(
        character if character.isalnum() else "-" for character in execution_id.lower()
    ).strip("-")
    hre_root = (
        Path(tempfile.gettempdir()).resolve()
        / "mnemos-phase1-evidence-hre-v2"
        / snapshot_hash
        / safe_execution_id
    )
    if hre_root.exists():
        raise RuntimeError(f"stale or concurrent Phase 1 HRE root: {hre_root}")
    hre_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    base_environment = dict(os.environ)
    base_environment["PYTHONPATH"] = str(cwd)
    run = HermeticRunEnvironment.create(
        hre_root,
        profile="isolated",
        base_environment=base_environment,
        inherit_credentials=False,
    )
    env = dict(run.environment)
    env["MNEMOS_TEST_RUN"] = "1"
    try:
        completed = subprocess.run(
            [
                str(runtime),
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-vv",
                "--tb=no",
                *node_ids,
            ],
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        formal_diff = run.finalize()
        manifest_payload = json.loads(read_native_bytes(run.manifest_path).decode("utf-8"))
        manifest_verified = verify_environment_manifest(
            manifest_payload,
            run.environment,
        )
        hermetic_report = {
            "profile": run.profile,
            "environment_hash": run.environment_hash,
            "outside_write_count": len(formal_diff),
            "formal_state_diff": formal_diff,
            "credentials_inherited": False,
            "manifest_verified": manifest_verified,
            "manifest_integrity_digest": manifest_payload.get("integrity", {}).get("digest"),
        }
    finally:
        shutil.rmtree(hre_root)
    outcomes = _parse_outcomes(completed.stdout)
    result = {
        "execution_id": execution_id,
        "exit_code": completed.returncode,
        "outcomes": outcomes,
        "executed_node_count": sum(len(outcomes[key]) for key in _CREDIT_OUTCOMES),
        "hermetic_run": hermetic_report,
    }
    if completed.returncode != 0 or not _has_valid_hermetic_run(result):
        result["diagnostic_output_tail"] = "\n".join(completed.stdout.splitlines()[-120:])
    return result


def _has_noncredit(execution: dict[str, Any]) -> bool:
    outcomes = execution.get("outcomes", {})
    return any(outcomes.get(key) for key in _NONCREDIT_OUTCOMES)


def _has_valid_hermetic_run(execution: dict[str, Any]) -> bool:
    report = execution.get("hermetic_run", {})
    return bool(
        report.get("profile") == "isolated"
        and isinstance(report.get("environment_hash"), str)
        and len(report["environment_hash"]) == 64
        and report.get("outside_write_count") == 0
        and report.get("formal_state_diff") == []
        and report.get("credentials_inherited") is False
        and report.get("manifest_verified") is True
        and isinstance(report.get("manifest_integrity_digest"), str)
        and len(report["manifest_integrity_digest"]) == 64
    )


def _require_candidate_green(
    requirement_id: str,
    execution: dict[str, Any],
) -> None:
    outcomes = execution["outcomes"]
    if (
        execution["exit_code"] != 0
        or outcomes["failed"]
        or _has_noncredit(execution)
        or execution["executed_node_count"] == 0
        or not _has_valid_hermetic_run(execution)
    ):
        raise RuntimeError(
            f"{requirement_id} candidate exact oracles are not green: "
            f"exit_code={execution.get('exit_code')!r}, "
            f"outcomes={execution.get('outcomes')!r}, "
            f"hermetic_run={execution.get('hermetic_run')!r}, "
            f"diagnostic_tail={execution.get('diagnostic_output_tail')!r}"
        )


def _mutation_kill_failure(
    requirement_id: str,
    execution: dict[str, Any],
    killing_node_ids: tuple[str, ...],
) -> str | None:
    outcomes = execution["outcomes"]
    failed_nodes = set(outcomes["failed"])
    killing_nodes_failed = all(
        any(failed == required or failed.startswith(required + "[") for failed in failed_nodes)
        for required in killing_node_ids
    )
    if (
        execution["exit_code"] != 1
        or not outcomes["failed"]
        or not killing_node_ids
        or not killing_nodes_failed
        or _has_noncredit(execution)
        or not _has_valid_hermetic_run(execution)
    ):
        execution_id = str(execution.get("execution_id") or requirement_id)
        return (
            f"{execution_id} declared structural mutation was not cleanly killed: "
            f"exit_code={execution.get('exit_code')!r}, "
            f"failed={outcomes.get('failed')!r}, "
            f"noncredit={{{', '.join(name for name in _NONCREDIT_OUTCOMES if outcomes.get(name))}}}, "
            f"declared_killing_nodes={killing_node_ids!r}"
        )
    return None


def _require_mutation_killed(
    requirement_id: str,
    execution: dict[str, Any],
    killing_node_ids: tuple[str, ...],
) -> None:
    failure = _mutation_kill_failure(
        requirement_id,
        execution,
        killing_node_ids,
    )
    if failure is not None:
        raise RuntimeError(failure)


def _require_all_mutations_killed(failures: list[str]) -> None:
    """Report the complete survivor set after every declared mutation ran."""
    if failures:
        raise RuntimeError(
            f"Phase 1 mutation execution found {len(failures)} survivor(s):\n"
            + "\n".join(f"- {failure}" for failure in failures)
        )


@contextmanager
def _candidate_tree(
    source_paths: tuple[str, ...],
) -> Iterator[Path]:
    source_identity = _sha256_bytes(_canonical_bytes(source_paths))
    destination = (
        Path(tempfile.gettempdir()).resolve()
        / "mnemos-phase1-evidence-candidate-v2"
        / source_identity
    )
    if destination.exists():
        raise RuntimeError(f"stale or concurrent Phase 1 candidate root: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.mkdir(mode=0o700)
    try:
        _materialize_current_tree(destination, source_paths)
        _initialize_candidate_repository(destination)
        yield destination
    finally:
        shutil.rmtree(destination)


def build_artifact() -> dict[str, Any]:
    """Execute the frozen candidate and all declared mutation-kill oracles."""
    runs: dict[str, Any] = {}
    mutation_failures: list[str] = []
    specs = _phase1_specs()
    source_paths = _current_source_paths()
    snapshot_paths = _execution_snapshot_paths(source_paths, specs)
    live_snapshot_before = _snapshot(ROOT, snapshot_paths)
    with _candidate_tree(source_paths) as candidate_root:
        frozen_snapshot = _snapshot(candidate_root, snapshot_paths)
        if frozen_snapshot != live_snapshot_before:
            raise RuntimeError("frozen Phase 1 candidate snapshot differs from current tree")
        for spec in specs:
            requirement_id = str(spec["requirement_id"])
            root_id = _root_id(spec)
            node_ids = tuple(dict.fromkeys(str(node) for node in spec["node_ids"]))
            mutation_candidate_paths = tuple(
                dict.fromkeys(str(path) for path in spec["mutation_candidate_paths"])
            )
            mutation_ids = tuple(str(value) for value in spec["mutation_operator_ids"])
            killing_node_ids = tuple(str(value) for value in spec["mutation_oracle_node_ids"])
            killing_nodes_by_operator = {
                str(operator_id): tuple(str(node) for node in nodes)
                for operator_id, nodes in spec["mutation_oracle_node_ids_by_operator"].items()
            }
            if set(killing_nodes_by_operator) != set(mutation_ids):
                raise RuntimeError(f"{requirement_id} mutation oracle map is incomplete")
            oracle_paths = tuple(sorted({node.split("::", 1)[0] for node in node_ids}))
            oracle_materialization = [
                _path_identity(candidate_root, relative) for relative in oracle_paths
            ]
            candidate = _run_nodes(
                candidate_root,
                node_ids,
                execution_id=f"{requirement_id}-candidate",
                snapshot_hash=frozen_snapshot["sha256"],
            )
            _require_candidate_green(requirement_id, candidate)
            candidate.pop("diagnostic_output_tail", None)
            if _snapshot(candidate_root, snapshot_paths) != frozen_snapshot:
                raise RuntimeError(
                    f"{requirement_id} candidate oracle mutated the frozen source tree"
                )
            source_replacements = {
                str(item["operator_id"]): item
                for item in spec.get("mutation_source_replacements", ())
            }
            mutation_executions: dict[str, Any] = {}
            for mutation_id in mutation_ids:
                operator_killing_nodes = killing_nodes_by_operator[mutation_id]
                source_replacement = source_replacements.get(mutation_id)
                if isinstance(source_replacement, dict):
                    changes, originals = _apply_source_replacement_mutation(
                        candidate_root,
                        source_replacement,
                    )
                    mutation_strategy = "exact_source_replacement"
                    baseline_commit: str | None = None
                else:
                    changes, originals = _apply_historical_mutation(
                        candidate_root,
                        baseline_commit=PHASE1_BASELINE_COMMITS[root_id],
                        candidate_paths=mutation_candidate_paths,
                    )
                    mutation_strategy = "historical_implementation_revert"
                    baseline_commit = PHASE1_BASELINE_COMMITS[root_id]
                try:
                    mutation = _run_nodes(
                        candidate_root,
                        operator_killing_nodes,
                        execution_id=f"{requirement_id}-mutation-{mutation_id}",
                        snapshot_hash=frozen_snapshot["sha256"],
                    )
                finally:
                    _restore_candidate(candidate_root, originals)
                mutation_failure = _mutation_kill_failure(
                    requirement_id,
                    mutation,
                    operator_killing_nodes,
                )
                if _snapshot(candidate_root, snapshot_paths) != frozen_snapshot:
                    raise RuntimeError(
                        f"{requirement_id} mutation execution did not restore " "the candidate tree"
                    )
                if mutation_failure is not None:
                    mutation_failures.append(mutation_failure)
                    continue
                mutation.pop("diagnostic_output_tail", None)
                mutation_payload = {
                    "operator_id": mutation_id,
                    "strategy": mutation_strategy,
                    "baseline_commit": baseline_commit,
                    "changed_artifacts": changes,
                }
                mutation_executions[mutation_id] = {
                    "mutation": mutation_payload,
                    "mutation_hash": _sha256_bytes(_canonical_bytes(mutation_payload)),
                    "oracle_binding": {
                        "declared_killing_node_ids": sorted(operator_killing_nodes),
                        "observed_failed_node_ids": sorted(mutation["outcomes"]["failed"]),
                    },
                    "execution": mutation,
                    "status": "killed",
                }
            runs[requirement_id] = {
                "root_id": root_id,
                "selected_nodes": list(node_ids),
                "oracle_materialization": oracle_materialization,
                "candidate_snapshot": frozen_snapshot,
                "fault_model_ids": list(spec.get("fault_model_ids", ())),
                "risk_scenario_ids": list(spec.get("risk_scenario_ids", ())),
                "risk_scenario_evidence_role": spec.get("risk_scenario_evidence_role"),
                "mutation_oracle_node_ids": list(killing_node_ids),
                "mutation_oracle_node_ids_by_operator": {
                    operator_id: list(nodes)
                    for operator_id, nodes in killing_nodes_by_operator.items()
                },
                "mutation_operator_ids": list(mutation_ids),
                "candidate_execution": candidate,
                "mutation_executions": mutation_executions,
                "kill_summary": {
                    "executed_operator_ids": sorted(mutation_ids),
                    "killed_operator_ids": sorted(mutation_ids),
                    "survived_operator_ids": [],
                    "kill_rate_percent": 100,
                },
            }
    if _snapshot(ROOT, snapshot_paths) != live_snapshot_before:
        raise RuntimeError("current Phase 1 execution snapshot changed during evidence run")
    _require_all_mutations_killed(mutation_failures)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "execution_boundary": {
            "runtime": ".venv/bin/python",
            "network": "not_required",
            "production_state": "not_read_or_written",
            "temporary_candidate_tree": True,
            "mutation_scope": "declared_candidate_artifacts_only",
            "mutation_oracle_selection": "declared_killing_nodes_only",
            "raw_pytest_output_excluded_from_identity": True,
        },
        "candidate_snapshot": {
            **frozen_snapshot,
            "scope": "phase1_code_tests_and_exact_candidate_artifacts",
        },
        "runs": runs,
        "denominator_summary": phase1_execution_denominator_summary(runs),
    }
    payload["evidence_hash"] = _sha256_bytes(_canonical_bytes(payload))
    return payload


def _publish_artifact(output: Path, artifact: dict[str, Any]) -> None:
    try:
        secure_atomic_write_bytes(
            output.parent,
            output.name,
            _canonical_bytes(artifact) + b"\n",
        )
    except DurableIOError as exc:
        raise RuntimeError(
            f"phase1_execution_artifact_publish_failed:{exc}"
        ) from None
    except OSError:
        raise RuntimeError(
            "phase1_execution_artifact_publish_failed:os_error"
        ) from None


def main(argv: list[str] | None = None) -> int:
    """Generate and optionally persist the current Phase 1 evidence artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=(ROOT / "docs" / "acceptance" / "phase1_historical_defect_execution_evidence.json"),
    )
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    artifact = build_artifact()
    if args.write:
        _publish_artifact(args.output, artifact)
    print(
        json.dumps(
            {
                "ok": True,
                "written": args.write,
                "output": str(args.output),
                "evidence_hash": artifact["evidence_hash"],
                "requirements": sorted(artifact["runs"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
