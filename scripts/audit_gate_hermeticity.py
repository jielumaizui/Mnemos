#!/usr/bin/env python3
"""Audit test/gate entrypoints under one run-scoped hermetic environment."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ops.gate_execution import (  # noqa: E402
    GateExecutionEnvironment,
    GateRunnerSelector,
)

SCHEMA_VERSION = "mnemos.gate_hermeticity_audit.v1"
STRICT_SUITES = ("quick", "integration", "heavy", "full-score")


@dataclass(frozen=True)
class AuditCommand:
    command_id: str
    argv: tuple[str, ...]
    accepted_returncodes: tuple[int, ...] = (0,)


@dataclass(frozen=True)
class AuditResult:
    command_id: str
    argv: tuple[str, ...]
    returncode: int
    accepted_returncodes: tuple[int, ...]
    status: str
    stdout_path: str
    stderr_path: str


def build_command_plan(suites: Sequence[str], *, output_dir: Path) -> list[AuditCommand]:
    plan: list[AuditCommand] = []
    for suite in suites:
        if suite == "diagnostics":
            plan.extend(
                [
                    AuditCommand(
                        "health", ("python3", "mnemos_cli.py", "health", "--json"), (0, 1)
                    ),
                    AuditCommand(
                        "verify", ("python3", "scripts/verify_installation.py", "--json"), (0, 1)
                    ),
                    AuditCommand("status", ("python3", "mnemos_cli.py", "status")),
                    AuditCommand(
                        "distill-status", ("python3", "mnemos_cli.py", "distill", "status")
                    ),
                    AuditCommand(
                        "golden",
                        (
                            "python3",
                            "scripts/run_golden_benchmark.py",
                            "--strict",
                            "--mock-llm",
                        ),
                    ),
                ]
            )
        elif suite in {"quick", "integration", "heavy", "full"}:
            plan.append(
                AuditCommand(
                    f"tests-{suite}",
                    ("python3", "scripts/run_tests.py", suite),
                )
            )
        elif suite == "full-score":
            plan.append(
                AuditCommand(
                    "full-score",
                    (
                        "python3",
                        "scripts/run_full_score_gates.py",
                        "--strict",
                        "--output-dir",
                        "__GATE_ARTIFACTS__/full-score",
                    ),
                )
            )
        else:
            raise ValueError(f"unknown hermeticity suite: {suite}")
    return plan


def _slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _git_status() -> str:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git status failed")
    return completed.stdout


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--suite",
        action="append",
        choices=("diagnostics", "quick", "integration", "heavy", "full", "full-score"),
        help="repeat to select suites; strict defaults to quick/integration/heavy/full-score",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    suites = tuple(args.suite or (STRICT_SUITES if args.strict else ("diagnostics",)))
    output_dir = (
        args.output_dir.expanduser().resolve(strict=False)
        if args.output_dir
        else Path(tempfile.gettempdir()) / "mnemos-gate-hermeticity" / _slug()
    )
    if output_dir.exists() and (
        output_dir.is_symlink() or not output_dir.is_dir() or any(output_dir.iterdir())
    ):
        raise FileExistsError(f"gate hermeticity output root is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    plan = build_command_plan(suites, output_dir=output_dir)
    repo_before = _git_status()
    results: list[AuditResult] = []
    gate_environments: list[dict[str, object]] = []
    all_formal_diff: list[str] = []
    for item in plan:
        if len(item.argv) < 2 or item.argv[0] not in {"python", "python3"}:
            raise ValueError(f"unsupported untyped gate command: {item.argv}")
        execution = GateExecutionEnvironment(
            base_root=output_dir / "gates",
            repo_root=ROOT,
            gate_id=item.command_id,
        )
        artifacts = execution.run.environment["MNEMOS_RUN_ARTIFACTS_DIR"]
        selector = GateRunnerSelector(
            runner_kind="python",
            entrypoint=item.argv[1],
            argv=tuple(value.replace("__GATE_ARTIFACTS__", artifacts) for value in item.argv[2:]),
        )
        completed = execution.execute(selector)
        all_formal_diff.extend(completed.formal_state_diff)
        gate_environments.append(
            {
                "gate_id": item.command_id,
                "sandbox_root": completed.sandbox_root,
                "environment_hash": completed.environment_hash,
                "os_write_guard": completed.os_write_guard,
                "formal_state_diff": list(completed.formal_state_diff),
            }
        )
        results.append(
            AuditResult(
                command_id=item.command_id,
                argv=item.argv,
                returncode=completed.returncode,
                accepted_returncodes=item.accepted_returncodes,
                status=(
                    "passed" if completed.returncode in item.accepted_returncodes else "failed"
                ),
                stdout_path=completed.stdout_path,
                stderr_path=completed.stderr_path,
            )
        )

    formal_diff = sorted(set(all_formal_diff))
    repo_after = _git_status()
    repo_diff = [] if repo_after == repo_before else ["git-status"]
    outside_diff = sorted([*formal_diff, *repo_diff])
    ok = all(result.status == "passed" for result in results) and not outside_diff
    payload = {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "strict": bool(args.strict),
        "suites": list(suites),
        "sandbox_root": str(output_dir),
        "per_gate_environment_count": len(gate_environments),
        "unique_sandbox_count": len({str(item["sandbox_root"]) for item in gate_environments}),
        "gate_environments": gate_environments,
        "outside_write_count": len(outside_diff),
        "formal_state_diff": formal_diff,
        "repo_state_diff": repo_diff,
        "results": [asdict(result) for result in results],
    }
    report_path = output_dir / "gate_hermeticity.json"
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"Gate hermeticity: ok={ok} suites={','.join(suites)} "
            f"outside_write_count={len(outside_diff)}"
        )
        print(f"Report: {report_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
