"""Single process-environment seam for runtime configuration and path discovery."""

from __future__ import annotations

import os
from collections.abc import ItemsView
from pathlib import Path
from typing import TypeVar, overload


_ENVIRONMENT = os.environ
_T = TypeVar("_T")


@overload
def environment_get(name: str) -> str | None: ...


@overload
def environment_get(name: str, default: _T) -> str | _T: ...


def environment_get(name: str, default=None):
    return _ENVIRONMENT.get(name, default)


def environment_items() -> ItemsView[str, str]:
    return _ENVIRONMENT.items()


def environment_snapshot() -> dict[str, str]:
    return dict(_ENVIRONMENT)


def environment_set(name: str, value: str) -> None:
    """Set one process-local environment value through the canonical owner."""

    _ENVIRONMENT[name] = value


def auto_type_environment_value(value: str):
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def run_owned_override_is_stale(env_var: str, marker_var: str) -> bool:
    value = environment_get(env_var)
    marker = environment_get(marker_var)
    default_mnemos = environment_get("MNEMOS_RUN_DEFAULT_MNEMOS_DIR")
    active_mnemos = environment_get("MNEMOS_DIR")
    return bool(
        value
        and marker
        and value == marker
        and default_mnemos
        and active_mnemos
        and Path(active_mnemos).expanduser().resolve(strict=False)
        != Path(default_mnemos).expanduser().resolve(strict=False)
    )
