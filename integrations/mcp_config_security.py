"""Atomic security boundary for MCP host config and capability rotation."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from core.agent_kit.authorization import (
    AgentAuthorizationStore,
    MCPLaunchCredentialStore,
)


def _secure_config(path: Path) -> None:
    """Restrict local MCP config and its one-time migration backup."""
    if os.name != "nt" and path.exists():
        path.chmod(0o600)
        backup_path = path.with_name(path.name + ".mnemos.bak")
        if backup_path.exists():
            backup_path.chmod(0o600)


def reference_resolves(
    reference: str,
    authorization_store: AgentAuthorizationStore | None,
    credential_store: MCPLaunchCredentialStore | None,
) -> bool:
    """Return whether a keyring reference resolves to an active principal."""
    if not reference:
        return False
    secret_store = credential_store or MCPLaunchCredentialStore()
    try:
        credential = secret_store.resolve(reference)
    except (ImportError, OSError, RuntimeError, ValueError):
        return False
    if not credential:
        return False
    store = authorization_store or AgentAuthorizationStore(initialize=False)
    return store.resolve_mcp_principal(credential) is not None


def _activate_rotation(
    path: Path,
    *,
    written: bool,
    store: AgentAuthorizationStore,
    credential_store: MCPLaunchCredentialStore,
    previous_reference: str,
    previous_credential: str,
    new_reference: str,
    new_credential: str,
) -> bool:
    """Activate the prepared credential only after its config is durable."""
    if not written:
        return False
    _secure_config(path)
    previous_capability_id = ""
    if previous_credential:
        previous_capability_id = previous_credential.partition(".")[0]
    elif previous_reference and previous_reference != new_reference:
        try:
            previous_capability_id = credential_store.capability_id_from_reference(
                previous_reference
            )
        except ValueError:
            previous_capability_id = ""
    activated = store.activate_mcp_capability_rotation(
        new_credential,
        previous_capability_id=previous_capability_id,
    )
    if not activated:
        raise RuntimeError("MCP capability rotation activation failed")
    if previous_reference and previous_reference != new_reference:
        try:
            credential_store.delete(previous_reference)
        except (ImportError, OSError, RuntimeError, ValueError):
            pass
    return True


def _write_sensitive_text(
    path: Path,
    text: str,
    *,
    backup: bool = True,
    backup_redact_values: tuple[str, ...] = (),
) -> tuple[bool, str | None]:
    """Atomically write a config and return its pre-write text for rollback."""
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        _secure_config(path)
        return True, old
    if backup and old is not None:
        backup_path = path.with_name(path.name + ".mnemos.bak")
        if backup_path.exists() and any(
            secret and secret in old for secret in backup_redact_values
        ):
            backup_path.unlink()
        if not backup_path.exists():
            backup_text = old
            for secret in backup_redact_values:
                if secret:
                    backup_text = backup_text.replace(
                        secret,
                        "<removed-during-mcp-migration>",
                    )
            fd = os.open(
                backup_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(backup_text)
                handle.flush()
                os.fsync(handle.fileno())

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.mnemos-",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _secure_config(path)
        return True, old
    except (OSError, UnicodeError):
        temp_path.unlink(missing_ok=True)
        raise


def commit_rotated_config(
    path: Path,
    text: str,
    *,
    store: AgentAuthorizationStore,
    credential_store: MCPLaunchCredentialStore,
    previous_reference: str,
    previous_credential: str,
    new_reference: str,
    new_credential: str,
) -> bool:
    """Commit config and capability rotation with fail-closed recovery."""
    try:
        written, previous_text = _write_sensitive_text(
            path,
            text,
            backup_redact_values=(previous_credential,),
        )
    except (OSError, UnicodeError):
        try:
            credential_store.delete(new_reference)
        except (ImportError, OSError, RuntimeError, ValueError):
            pass
        store.revoke_mcp_capability(new_credential)
        raise
    try:
        return _activate_rotation(
            path,
            written=written,
            store=store,
            credential_store=credential_store,
            previous_reference=previous_reference,
            previous_credential=previous_credential,
            new_reference=new_reference,
            new_credential=new_credential,
        )
    except (OSError, RuntimeError, ValueError):
        store.revoke_mcp_capability(new_credential)
        try:
            credential_store.delete(new_reference)
        except (ImportError, OSError, RuntimeError, ValueError):
            pass
        if previous_text is not None:
            try:
                _write_sensitive_text(path, previous_text, backup=False)
            except (OSError, UnicodeError):
                pass
        raise
