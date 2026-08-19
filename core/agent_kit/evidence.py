"""Installation evidence helpers derived from the support manifest."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional, Tuple

from core.agent_kit.source_support_manifest import (
    AgentSourceSupportManifestError,
    expand_path_templates,
    get_agent_source_support_manifest,
)
from core.ops.durable_io import inspect_path_kind


def _agent_home() -> Path:
    """Indirection retained for hermetic installation-evidence tests."""
    return Path.home()


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(base.resolve(strict=False))
        return True
    except ValueError:
        return False


def agent_install_evidence(agent_name: str) -> Tuple[bool, Optional[str]]:
    """Return whether a manifest-declared local source appears installed.

    Mnemos setup may create MCP configuration directories for agents that are
    not installed yet.  The manifest therefore declares only native CLI and
    transcript/database evidence paths, never active-policy files alone.
    """

    manifest = get_agent_source_support_manifest()
    try:
        spec = manifest.require_active_source(agent_name)
    except AgentSourceSupportManifestError:
        return False, None
    install_evidence = spec.payload["install_evidence"]
    for cli_name in install_evidence.get("cli_names", []):
        found = shutil.which(str(cli_name))
        if found:
            return True, found

    home = _agent_home()
    cwd = Path.cwd()
    templates = list(install_evidence.get("path_templates", []))
    if not _is_under(cwd, home):
        templates = [template for template in templates if "{cwd}" not in str(template)]
    for path in expand_path_templates(
        templates,
        home=home,
        cwd=cwd,
    ):
        if inspect_path_kind(path) != "missing":
            return True, str(path)

    return False, None
