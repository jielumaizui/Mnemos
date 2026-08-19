"""Scoped process-config authority for read-only command execution."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any


_CONFIG_OVERRIDE: ContextVar[Any | None] = ContextVar(
    "mnemos_config_override",
    default=None,
)


def current_config() -> Any | None:
    return _CONFIG_OVERRIDE.get()


@contextmanager
def use_config(config):
    token = _CONFIG_OVERRIDE.set(config)
    try:
        yield config
    finally:
        _CONFIG_OVERRIDE.reset(token)
