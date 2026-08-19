"""Deterministic benchmark contracts for Mnemos."""

from .golden import (
    DEFAULT_BASELINE_PATH,
    DEFAULT_MANIFEST_PATH,
    GOLDEN_BENCHMARK_SCHEMA_VERSION,
    audit_golden_benchmark_contract,
    build_golden_benchmark_health,
    run_golden_benchmark,
)

__all__ = [
    "DEFAULT_BASELINE_PATH",
    "DEFAULT_MANIFEST_PATH",
    "GOLDEN_BENCHMARK_SCHEMA_VERSION",
    "audit_golden_benchmark_contract",
    "build_golden_benchmark_health",
    "run_golden_benchmark",
]
