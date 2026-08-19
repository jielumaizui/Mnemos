"""OS-bound principal for explicit user actions in the local CLI process."""

from __future__ import annotations

import getpass
import os

from core.access_policy import AccessNarrowing, PrincipalEnvelope


def local_cli_identity(
    *,
    project: str = "mnemos",
    session_id: str = "",
) -> tuple[PrincipalEnvelope, AccessNarrowing]:
    """Resolve the invoking OS account into a narrow local-user capability."""

    normalized_project = str(project or "").strip().lower()
    if not normalized_project:
        raise PermissionError("local CLI feedback requires an exact project")
    uid = os.getuid()
    username = getpass.getuser().strip().lower()
    if not username:
        raise PermissionError("local CLI OS identity is unavailable")
    principal = PrincipalEnvelope(
        principal_id=f"local-user:{uid}:{username}",
        agent="mnemos-cli",
        host_kind="local_cli",
        capability_id=f"local-cli-os-account:{uid}",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({normalized_project}),
        source="local_os_account",
    )
    return principal, AccessNarrowing(
        session_id=str(session_id or "").strip(),
        project=normalized_project,
    )
