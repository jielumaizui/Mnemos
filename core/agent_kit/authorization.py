"""Read-only safe authorization state for discovered local agents."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from core.access_policy import PrincipalEnvelope
from core.config import get_config
from core.ops.durable_io import inspect_path_kind
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.utils import secure_directory, secure_file

AUTHORIZATION_STATES = {
    "detected",
    "probe_ok",
    "user_authorized",
    "shadow_enabled",
    "revoked",
    "blocked",
}
CONTENT_AUTHORIZED_STATES = {"user_authorized", "shadow_enabled"}
MCP_LAUNCH_KEYRING_SERVICE = "mnemos.mcp.launch"
MCP_LAUNCH_REFERENCE_PREFIX = f"keyring:{MCP_LAUNCH_KEYRING_SERVICE}/"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class AgentAuthorizationRecord:
    agent: str
    state: str
    directory: str = "*"
    capability: str = "content_analysis"
    purpose: str = "distillation"
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MCPPrincipalGrant:
    """Explicit server-side grant used when issuing one host capability."""

    agent: str
    state: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    allowed_projects: frozenset[str] = field(default_factory=frozenset)
    allowed_source_agents: frozenset[str] = field(default_factory=frozenset)
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)


class MCPLaunchCredentialStore:
    """Store MCP launch secrets in the OS keyring and expose references only."""

    service_name = MCP_LAUNCH_KEYRING_SERVICE
    reference_prefix = MCP_LAUNCH_REFERENCE_PREFIX

    @classmethod
    def reference_for(cls, agent: str, credential: str) -> str:
        """Build a non-secret reference containing the host and capability id."""
        capability_id, separator, _ = str(credential or "").partition(".")
        normalized_agent = str(agent or "").strip().lower()
        if not normalized_agent or not separator or not capability_id:
            raise ValueError("agent and valid launch credential are required")
        return f"{cls.reference_prefix}{normalized_agent}/{capability_id}"

    @classmethod
    def _account_from_reference(cls, reference: str) -> str:
        value = str(reference or "").strip()
        if not value.startswith(cls.reference_prefix):
            raise ValueError("unsupported MCP launch credential reference")
        account = value[len(cls.reference_prefix) :]
        agent, separator, capability_id = account.partition("/")
        if not separator or not agent or not capability_id or "/" in capability_id:
            raise ValueError("invalid MCP launch credential reference")
        return account

    @classmethod
    def capability_id_from_reference(cls, reference: str) -> str:
        """Return the public capability id encoded in a validated reference."""
        return cls._account_from_reference(reference).rsplit("/", 1)[-1]

    def store(self, agent: str, credential: str) -> str:
        """Persist one opaque credential and return its non-secret reference."""
        import keyring  # type: ignore

        reference = self.reference_for(agent, credential)
        account = self._account_from_reference(reference)
        try:
            keyring.set_password(self.service_name, account, credential)
        except keyring.errors.KeyringError as exc:
            raise RuntimeError("MCP launch keyring write failed") from exc
        return reference

    def resolve(self, reference: str) -> str:
        """Resolve a keyring reference without exposing it in diagnostics."""
        import keyring  # type: ignore

        account = self._account_from_reference(reference)
        try:
            return str(keyring.get_password(self.service_name, account) or "")
        except keyring.errors.KeyringError as exc:
            raise RuntimeError("MCP launch keyring read failed") from exc

    def delete(self, reference: str) -> bool:
        """Delete a referenced secret; missing entries are already deleted."""
        import keyring  # type: ignore

        account = self._account_from_reference(reference)
        try:
            keyring.delete_password(self.service_name, account)
        except keyring.errors.PasswordDeleteError:
            return False
        except keyring.errors.KeyringError as exc:
            raise RuntimeError("MCP launch keyring delete failed") from exc
        return True


class InMemoryMCPLaunchCredentialStore(MCPLaunchCredentialStore):
    """Deterministic credential-reference store for tests and isolated probes."""

    def __init__(self):
        self._credentials: Dict[str, str] = {}

    def store(self, agent: str, credential: str) -> str:
        reference = self.reference_for(agent, credential)
        self._credentials[reference] = credential
        return reference

    def resolve(self, reference: str) -> str:
        self._account_from_reference(reference)
        return self._credentials.get(reference, "")

    def delete(self, reference: str) -> bool:
        self._account_from_reference(reference)
        return self._credentials.pop(reference, None) is not None


class AgentAuthorizationStore:
    """Persist agent authorization state without storing credentials or prompts."""

    def __init__(self, db_path: Path | None = None, *, initialize: bool = True):
        self.db_path = Path(db_path or get_config().database_dir / "agent_authorization.db")
        if initialize:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            secure_directory(self.db_path.parent)
            self._init_db()
            self._secure_storage_files()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._secure_storage_files()
        return conn

    def _secure_storage_files(self) -> None:
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            secure_file(path)

    def _connect_read_only(self) -> sqlite3.Connection:
        conn = connect_readonly_sqlite(self.db_path, timeout_seconds=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _store_is_missing(self) -> bool:
        return inspect_path_kind(self.db_path) == "missing"

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_authorizations (
                    agent TEXT NOT NULL,
                    directory TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (agent, directory, capability, purpose)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_launch_capabilities (
                    capability_id TEXT PRIMARY KEY,
                    secret_hash TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    host_kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    allowed_projects_json TEXT NOT NULL,
                    allowed_source_agents_json TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL DEFAULT '',
                    revoked_at TEXT NOT NULL DEFAULT '',
                    schema_version INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_principal_grants (
                    agent TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    allowed_projects_json TEXT NOT NULL,
                    allowed_source_agents_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def set_mcp_principal_grant(
        self,
        agent: str,
        *,
        capabilities: set[str] | frozenset[str],
        allowed_projects: set[str] | frozenset[str] = frozenset(),
        allowed_source_agents: set[str] | frozenset[str] = frozenset(),
        state: str = "active",
    ) -> MCPPrincipalGrant:
        """Create or replace an explicit MCP grant without storing a secret."""
        if state not in {"active", "revoked"}:
            raise ValueError(f"unsupported MCP grant state: {state}")
        normalized_agent = str(agent).strip().lower()
        if not normalized_agent:
            raise ValueError("agent is required")
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT created_at FROM mcp_principal_grants WHERE agent = ?",
                (normalized_agent,),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now
            conn.execute(
                """
                INSERT OR REPLACE INTO mcp_principal_grants (
                    agent, state, capabilities_json, allowed_projects_json,
                    allowed_source_agents_json, created_at, updated_at,
                    schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    normalized_agent,
                    state,
                    json.dumps(sorted(set(capabilities))),
                    json.dumps(sorted(set(allowed_projects))),
                    json.dumps(sorted(set(allowed_source_agents))),
                    created_at,
                    now,
                ),
            )
            if state == "active":
                conn.execute(
                    """
                    UPDATE mcp_launch_capabilities
                    SET state = 'revoked', revoked_at = ?
                    WHERE agent = ? AND state IN ('active', 'prepared')
                    """,
                    (now, normalized_agent),
                )
        return MCPPrincipalGrant(
            agent=normalized_agent,
            state=state,
            capabilities=frozenset(capabilities),
            allowed_projects=frozenset(allowed_projects),
            allowed_source_agents=frozenset(allowed_source_agents),
            created_at=created_at,
            updated_at=now,
        )

    def get_mcp_principal_grant(self, agent: str) -> MCPPrincipalGrant | None:
        """Return the explicit active/revoked grant for an MCP host."""
        if self._store_is_missing():
            return None
        with self._connect_read_only() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_principal_grants WHERE agent = ?",
                (str(agent).strip().lower(),),
            ).fetchone()
        if row is None or int(row["schema_version"] or 0) != 1:
            return None
        return MCPPrincipalGrant(
            agent=row["agent"],
            state=row["state"],
            capabilities=frozenset(json.loads(row["capabilities_json"])),
            allowed_projects=frozenset(json.loads(row["allowed_projects_json"])),
            allowed_source_agents=frozenset(
                json.loads(row["allowed_source_agents_json"])
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def revoke_mcp_principal_grant(self, agent: str) -> int:
        """Revoke one explicit grant and every active capability issued for it."""
        normalized_agent = str(agent).strip().lower()
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE mcp_principal_grants
                SET state = 'revoked', updated_at = ?
                WHERE agent = ?
                """,
                (now, normalized_agent),
            )
            cursor = conn.execute(
                """
                UPDATE mcp_launch_capabilities
                SET state = 'revoked', revoked_at = ?
                WHERE agent = ? AND state IN ('active', 'prepared')
                """,
                (now, normalized_agent),
            )
        self._secure_storage_files()
        return int(cursor.rowcount)

    def issue_mcp_capability(
        self,
        *,
        agent: str,
        host_kind: str,
        capabilities: set[str] | frozenset[str],
        allowed_projects: set[str] | frozenset[str] = frozenset(),
        allowed_source_agents: set[str] | frozenset[str] = frozenset(),
        expires_in_seconds: int = 365 * 24 * 60 * 60,
        state: str = "active",
    ) -> str:
        """Issue an opaque launch credential for a specific MCP host."""
        if state not in {"active", "prepared"}:
            raise ValueError(f"unsupported MCP capability state: {state}")
        capability_id = secrets.token_urlsafe(18)
        secret = secrets.token_urlsafe(32)
        now = _utc_now_iso()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(1, expires_in_seconds))
        ).replace(microsecond=0).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mcp_launch_capabilities (
                    capability_id, secret_hash, agent, host_kind, state,
                    capabilities_json, allowed_projects_json,
                    allowed_source_agents_json, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capability_id,
                    hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                    str(agent).strip().lower(),
                    str(host_kind).strip().lower(),
                    state,
                    json.dumps(sorted(set(capabilities))),
                    json.dumps(sorted(set(allowed_projects))),
                    json.dumps(sorted(set(allowed_source_agents))),
                    now,
                    expires_at,
                ),
            )
        return f"{capability_id}.{secret}"

    def activate_mcp_capability_rotation(
        self,
        new_credential: str,
        *,
        previous_capability_id: str = "",
    ) -> bool:
        """Atomically activate one prepared capability and revoke its predecessor."""
        capability_id, separator, secret = str(new_credential or "").partition(".")
        if not separator or not capability_id or not secret:
            return False
        candidate_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT secret_hash, agent, host_kind FROM mcp_launch_capabilities
                WHERE capability_id = ? AND state = 'prepared'
                """,
                (capability_id,),
            ).fetchone()
            if row is None or not hmac.compare_digest(
                candidate_hash,
                str(row["secret_hash"]),
            ):
                return False
            if previous_capability_id:
                previous = conn.execute(
                    """
                    SELECT agent, host_kind FROM mcp_launch_capabilities
                    WHERE capability_id = ?
                    """,
                    (previous_capability_id,),
                ).fetchone()
                if previous is not None and (
                    previous["agent"] != row["agent"]
                    or previous["host_kind"] != row["host_kind"]
                ):
                    return False
            conn.execute(
                """
                UPDATE mcp_launch_capabilities
                SET state = 'revoked', revoked_at = ?
                WHERE agent = ? AND host_kind = ? AND capability_id != ?
                  AND state IN ('active', 'prepared')
                """,
                (now, row["agent"], row["host_kind"], capability_id),
            )
            activated = conn.execute(
                """
                UPDATE mcp_launch_capabilities
                SET state = 'active'
                WHERE capability_id = ? AND state = 'prepared'
                """,
                (capability_id,),
            )
            if activated.rowcount != 1:
                conn.rollback()
                return False
        return True

    def resolve_mcp_principal(self, credential: str) -> PrincipalEnvelope | None:
        """Resolve a launch credential into an immutable server principal."""
        capability_id, separator, secret = str(credential or "").partition(".")
        if not separator or not capability_id or not secret or self._store_is_missing():
            return None
        with self._connect_read_only() as conn:
            row = conn.execute(
                """
                SELECT * FROM mcp_launch_capabilities
                WHERE capability_id = ? AND state = 'active'
                """,
                (capability_id,),
            ).fetchone()
        if row is None:
            return None
        if int(row["schema_version"] or 0) != 1:
            return None
        expires_at = str(row["expires_at"] or "")
        if not expires_at:
            return None
        try:
            expiry = datetime.fromisoformat(expires_at)
        except ValueError:
            return None
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            return None
        candidate_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(candidate_hash, row["secret_hash"]):
            return None
        return PrincipalEnvelope(
            principal_id=f"mcp:{row['host_kind']}:{row['agent']}:{capability_id}",
            agent=row["agent"],
            host_kind=row["host_kind"],
            capability_id=capability_id,
            capabilities=frozenset(json.loads(row["capabilities_json"])),
            allowed_projects=frozenset(json.loads(row["allowed_projects_json"])),
            allowed_source_agents=frozenset(
                json.loads(row["allowed_source_agents_json"])
            ),
            issued_at=str(row["issued_at"] or ""),
            expires_at=expires_at,
        )

    def revoke_mcp_capability(self, credential: str) -> bool:
        """Revoke the launch capability identified by an opaque credential."""
        capability_id, separator, _ = str(credential or "").partition(".")
        if not separator or not capability_id:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE mcp_launch_capabilities
                SET state = 'revoked', revoked_at = ?
                WHERE capability_id = ? AND state IN ('active', 'prepared')
                """,
                (_utc_now_iso(), capability_id),
            )
        return cursor.rowcount == 1

    def revoke_mcp_capability_id(self, capability_id: str) -> bool:
        """Revoke an active capability by its public id during reference cleanup."""
        normalized_id = str(capability_id or "").strip()
        if not normalized_id:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE mcp_launch_capabilities
                SET state = 'revoked', revoked_at = ?
                WHERE capability_id = ? AND state = 'active'
                """,
                (_utc_now_iso(), normalized_id),
            )
        return cursor.rowcount == 1

    def set_state(
        self,
        agent: str,
        state: str,
        *,
        directory: str = "*",
        capability: str = "content_analysis",
        purpose: str = "distillation",
    ) -> AgentAuthorizationRecord:
        if state not in AUTHORIZATION_STATES:
            raise ValueError(f"unsupported authorization state: {state}")
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT created_at FROM agent_authorizations
                WHERE agent = ? AND directory = ? AND capability = ? AND purpose = ?
                """,
                (agent, directory, capability, purpose),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_authorizations (
                    agent, directory, capability, purpose, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (agent, directory, capability, purpose, state, created_at, now),
            )
        return AgentAuthorizationRecord(
            agent=agent,
            directory=directory,
            capability=capability,
            purpose=purpose,
            state=state,
            created_at=created_at,
            updated_at=now,
        )

    def get_record(
        self,
        agent: str,
        *,
        directory: str = "*",
        capability: str = "content_analysis",
        purpose: str = "distillation",
    ) -> AgentAuthorizationRecord | None:
        if self._store_is_missing():
            return None
        with self._connect_read_only() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_authorizations
                WHERE agent = ? AND directory = ? AND capability = ? AND purpose = ?
                """,
                (agent, directory, capability, purpose),
            ).fetchone()
        if row is None:
            return None
        return AgentAuthorizationRecord(
            agent=row["agent"],
            directory=row["directory"],
            capability=row["capability"],
            purpose=row["purpose"],
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def content_access_authorized(state: str) -> bool:
        return state in CONTENT_AUTHORIZED_STATES
