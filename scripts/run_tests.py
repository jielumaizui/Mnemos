#!/usr/bin/env python3
"""Run Mnemos tests by feedback-loop layer.

Usage:
    python3 scripts/run_tests.py quick
    python3 scripts/run_tests.py integration -- -x --tb=short
    python3 scripts/run_tests.py system
    python3 scripts/run_tests.py heavy
    python3 scripts/run_tests.py full
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


LAYERS: Dict[str, List[str]] = {
    # Fast daily gate: unit tests plus root-level smoke/contract checks, excluding
    # wheel packaging because it builds a distribution and is consistently slower.
    "quick": [
        "tests/unit",
        "tests/static",
        "tests/test_smoke.py",
        "tests/test_functional.py",
        "tests/test_pipeline.py",
        "tests/test_audit_config_reads.py",
        "tests/test_arch_dependency_graph.py",
        "tests/test_delayed_imports.py",
        "tests/test_event_bus_map.py",
        "--ignore=tests/unit/test_packaging_contract.py",
        "-q",
    ],
    # Cross-module behavior without packaging or benchmark cost.
    "integration": [
        "tests/integration",
        "tests/acceptance",
        "tests/test_system.py",
        "-q",
    ],
    # Cross-platform CI entrypoint for the system-test surface. This intentionally
    # reuses the same HermeticRunEnvironment boundary as every other layer instead
    # of asking each workflow shell to create and export a temporary directory.
    "system": [
        "tests/test_system.py",
        "-v",
    ],
    # Known slow or specialized checks kept out of the daily feedback loop.
    "heavy": [
        "tests/unit/test_packaging_contract.py",
        "tests/benchmark",
        "tests/e2e",
        "-q",
    ],
    # Exact full suite.
    "full": [
        "tests",
        "-q",
    ],
}
_HERMETIC_HARNESS_OVERRIDE_OPTIONS = frozenset(
    {
        "-c",
        "--basetemp",
        "--confcutdir",
        "--noconftest",
        "--rootdir",
    }
)


def _pytest_files(directory: Path) -> set[Path]:
    return {
        path
        for pattern in ("test_*.py", "*_test.py")
        for path in directory.rglob(pattern)
        if path.is_file()
    }


def discover_test_files() -> set[str]:
    """Return the canonical pytest file denominator."""
    return {path.relative_to(ROOT).as_posix() for path in _pytest_files(ROOT / "tests")}


def layer_test_files() -> dict[str, set[str]]:
    """Resolve each release layer to the files it owns."""
    packaging = "tests/unit/test_packaging_contract.py"
    quick = {path.relative_to(ROOT).as_posix() for path in _pytest_files(ROOT / "tests" / "unit")}
    quick.discard(packaging)
    quick.update(
        path.relative_to(ROOT).as_posix() for path in _pytest_files(ROOT / "tests" / "static")
    )
    quick.update(
        item for item in LAYERS["quick"] if item.startswith("tests/test_") and item.endswith(".py")
    )
    integration = {
        path.relative_to(ROOT).as_posix()
        for directory in ("integration", "acceptance")
        for path in _pytest_files(ROOT / "tests" / directory)
    }
    integration.add("tests/test_system.py")
    heavy = {packaging}
    heavy.update(
        path.relative_to(ROOT).as_posix()
        for directory in ("benchmark", "e2e")
        for path in _pytest_files(ROOT / "tests" / directory)
    )
    return {"quick": quick, "integration": integration, "heavy": heavy}


def audit_layer_coverage() -> dict[str, object]:
    """Prove every pytest file belongs to exactly one release layer."""
    discovered = discover_test_files()
    layers = layer_test_files()
    ownership: dict[str, list[str]] = {}
    for layer, files in layers.items():
        for path in files:
            ownership.setdefault(path, []).append(layer)
    assigned = set(ownership)
    missing = sorted(discovered - assigned)
    extra = sorted(assigned - discovered)
    overlaps = {path: owners for path, owners in sorted(ownership.items()) if len(owners) != 1}
    return {
        "schema_version": "mnemos.test_suite_denominator.v1",
        "ok": not missing and not extra and not overlaps,
        "discovered_count": len(discovered),
        "assigned_count": len(assigned & discovered),
        "layer_counts": {name: len(files) for name, files in layers.items()},
        "missing": missing,
        "extra": extra,
        "overlaps": overlaps,
    }


def build_pytest_command(layer: str, extra_args: List[str]) -> List[str]:
    """Build the pytest command for a named layer."""
    if layer not in LAYERS:
        valid = ", ".join(sorted(LAYERS))
        raise ValueError(f"unknown test layer: {layer}; valid layers: {valid}")
    for argument in extra_args:
        option = argument.split("=", 1)[0]
        forbidden_long_option_prefix = option.startswith("--") and any(
            forbidden.startswith(option)
            for forbidden in _HERMETIC_HARNESS_OVERRIDE_OPTIONS
            if forbidden.startswith("--")
        )
        if (
            forbidden_long_option_prefix
            or option in _HERMETIC_HARNESS_OVERRIDE_OPTIONS
            or (argument.startswith("-c") and not argument.startswith("--"))
        ):
            raise ValueError(f"pytest option cannot override the hermetic harness: {option}")
    return [sys.executable, "-m", "pytest", *LAYERS[layer], *extra_args]


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Mnemos tests by layer.")
    parser.add_argument(
        "layer",
        nargs="?",
        default="quick",
        choices=sorted(LAYERS),
        help="Test layer to run. Defaults to quick.",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Extra pytest args. Prefix with -- to separate from script args.",
    )
    args = parser.parse_args(argv)

    extra_args = list(args.pytest_args)
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]

    cmd = build_pytest_command(args.layer, extra_args)
    print("Running:", " ".join(cmd))
    run_environment = None
    child_environment = dict(os.environ)
    if not child_environment.get("MNEMOS_RUN_ENVIRONMENT_HASH"):
        from core.ops.hermetic_run import HermeticRunEnvironment

        run_environment = HermeticRunEnvironment.create(
            Path(tempfile.mkdtemp(prefix=f"mnemos-tests-{args.layer}-")),
            profile="isolated",
            base_environment=child_environment,
        )
        child_environment = dict(run_environment.environment)
        print(f"Environment manifest: {run_environment.manifest_path}")
    child_environment["MNEMOS_TEST_RUN"] = "1"
    child_environment["PYTEST_ADDOPTS"] = ""

    returncode = subprocess.run(cmd, env=child_environment).returncode
    if run_environment is not None and run_environment.finalize():
        return 1
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
