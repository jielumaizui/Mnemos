#!/usr/bin/env python3
"""Compatibility facade for cognitive-successor D0 catalog generation."""

from __future__ import annotations

from typing import Any

from .successor_d0_generation import builder as _builder
from .successor_d0_generation import cli_inventory as _cli_inventory
from .successor_d0_generation import contract_inventory as _contract_inventory
from .successor_d0_generation import model as _model
from .successor_d0_generation import repository_inventory as _repository_inventory
from .successor_d0_generation import runtime_inventory as _runtime_inventory
from .successor_d0_generation import snapshot as _snapshot
from .successor_d0_generation import static_python as _static_python

CatalogBundle = _model.CatalogBundle
CatalogInputError = _model.CatalogInputError
CatalogRequest = _model.CatalogRequest
SuccessorD0Catalog = _builder.SuccessorD0Catalog

# Existing callers use these two helpers; they remain compatibility attributes
# without enlarging the declared public Interface below.
ARTIFACT_ORDER = _model.ARTIFACT_ORDER
sha256_bytes = _model.sha256_bytes

_COMPATIBILITY_MODULES = (
    _builder,
    _cli_inventory,
    _contract_inventory,
    _model,
    _repository_inventory,
    _runtime_inventory,
    _snapshot,
    _static_python,
)


def __getattr__(name: str) -> Any:
    for module in _COMPATIBILITY_MODULES:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    compatibility_names = {name for module in _COMPATIBILITY_MODULES for name in vars(module)}
    return sorted(set(globals()) | compatibility_names)


__all__ = [
    "CatalogBundle",
    "CatalogInputError",
    "CatalogRequest",
    "SuccessorD0Catalog",
]
