#!/usr/bin/env python3
"""Compatibility facade for the independent successor D0 verifier."""

from __future__ import annotations

from typing import Any

from .successor_d0_verification import census as _census
from .successor_d0_verification import closure as _closure
from .successor_d0_verification import runner as _runner
from .successor_d0_verification import snapshot as _snapshot
from .successor_d0_verification import wire as _wire

Finding = _wire.Finding
main = _runner.main
verify_bundle = _runner.verify_bundle

_COMPATIBILITY_MODULES = (_census, _closure, _runner, _snapshot, _wire)


def __getattr__(name: str) -> Any:
    for module in _COMPATIBILITY_MODULES:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    compatibility_names = {name for module in _COMPATIBILITY_MODULES for name in vars(module)}
    return sorted(set(globals()) | compatibility_names)


__all__ = ["Finding", "main", "verify_bundle"]
