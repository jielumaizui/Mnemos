"""Read access policy for agent-visible memory and wiki search results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.utils import read_bytes_value

_RESTRICTED_SCOPES = {"private", "project", "framework"}
_ACL_KEYS = frozenset(
    {
        "scope",
        "source_agent",
        "session_id",
        "project",
        "acl_schema_version",
        "acl_metadata_complete",
        "acl_reconciliation_status",
    }
)
ACL_METADATA_KEYS = _ACL_KEYS
_KNOWN_SOURCE_AGENTS = frozenset(
    {
        "aider",
        "claude",
        "codex",
        "crush",
        "cursor",
        "gemini",
        "hermes",
        "human",
        "kimi",
        "kiro",
        "openclaw",
        "opencode",
        "system",
        "windsurf",
    }
)
_ACL_SCOPES = frozenset(
    {"agent", "framework", "global", "private", "project", "public", "restricted"}
)
_TRUSTED_ACL_STATUSES = frozenset(
    {"canonical_raw_index", "proven", "provenance_write", "server_principal"}
)


@dataclass(frozen=True)
class _ACLPlanItem:
    """One immutable ACL repair prepared before any source file is changed."""

    path: Path
    kind: str
    root: Path
    category: str
    content: str
    original_sha256: str
    desired_sha256: str


@dataclass(frozen=True)
class WikiProjectionBatchReceipt:
    """Proof that one direct Wiki ACL batch entered lifecycle and EventBus."""

    update_count: int
    mutation_count: int
    event_count: int
    backup_manifest: str
    source: str


@dataclass(frozen=True)
class AccessContext:
    """Caller identity used when filtering memory results."""

    agent: str = ""
    session_id: str = ""
    project: str = ""
    allow_cross_agent: bool = False
    authorized_agents: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class PrincipalEnvelope:
    """Server-resolved identity and grants for one MCP process."""

    principal_id: str
    agent: str
    host_kind: str
    capability_id: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    allowed_projects: frozenset[str] = field(default_factory=frozenset)
    allowed_source_agents: frozenset[str] = field(default_factory=frozenset)
    source: str = "server"
    issued_at: str = ""
    expires_at: str = ""


@dataclass(frozen=True)
class AccessNarrowing:
    """Request-scoped filters that may only narrow a server principal."""

    session_id: str = ""
    project: str = ""


@dataclass(frozen=True)
class AuthorizedToolCall:
    """Authorization result produced before an MCP handler can execute."""

    allowed: bool
    reason: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    policy: str = ""


class ACLReconciler:
    """Backfill ACL envelopes from canonical provenance, otherwise restrict."""

    def __init__(
        self,
        *,
        wiki_dir: Path,
        raw_dir: Path,
        wiki_projection_commit: Callable[[Path, int], WikiProjectionBatchReceipt] | None = None,
    ):
        self.wiki_dir = Path(wiki_dir)
        self.raw_dir = Path(raw_dir)
        self._wiki_projection_commit = wiki_projection_commit

    def reconcile(
        self,
        *,
        apply: bool = False,
        targets: Sequence[str] | None = None,
        backup_dir: Path | None = None,
    ) -> Dict[str, Any]:
        """Scan Wiki/raw Markdown and optionally apply deterministic ACL fields."""

        if apply and targets is None:
            raise ValueError("ACL apply requires explicit wiki/raw targets")
        normalized_targets = tuple(
            dict.fromkeys(str(value) for value in (targets or ("wiki", "raw")))
        )
        if not normalized_targets or set(normalized_targets) - {"wiki", "raw"}:
            raise ValueError("ACL reconciliation targets must be wiki and/or raw")
        if apply and backup_dir is None:
            raise ValueError("ACL apply requires an explicit recovery backup directory")
        backup_dir = Path(backup_dir) if backup_dir is not None else None
        if apply:
            assert backup_dir is not None
            self._validate_backup_dir(backup_dir, normalized_targets)
        canonical_raw_provenance = self._canonical_raw_provenance()
        report: dict[str, Any] = {
            "total": 0,
            "would_change": 0,
            "changed": 0,
            "proven": 0,
            "restricted": 0,
            "parse_errors": 0,
            "unresolved": 0,
        }
        plan: List[_ACLPlanItem] = []
        roots = {"wiki": self.wiki_dir, "raw": self.raw_dir}
        for kind in normalized_targets:
            root = roots[kind]
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.md")):
                report["total"] += 1
                status, rendered = self._plan_file(
                    path,
                    kind=kind,
                    canonical_raw_provenance=canonical_raw_provenance,
                )
                if status == "parse_error":
                    report["parse_errors"] += 1
                    report["unresolved"] += 1
                    continue
                report["proven" if status.startswith("proven") else "restricted"] += 1
                if status.endswith(":change"):
                    report["would_change"] += 1
                    assert rendered is not None
                    original_sha256 = self._sha256_path(path)
                    plan.append(
                        _ACLPlanItem(
                            path=path,
                            kind=kind,
                            root=root,
                            category=status.split(":", 1)[0],
                            content=rendered,
                            original_sha256=original_sha256,
                            desired_sha256=self._sha256_bytes(rendered.encode("utf-8")),
                        )
                    )
        if apply and report["unresolved"]:
            raise ValueError("ACL apply refused because the dry-run plan contains unresolved files")
        if apply:
            assert backup_dir is not None
            if plan:
                wiki_change_count = sum(item.kind == "wiki" for item in plan)
                if wiki_change_count and self._wiki_projection_commit is None:
                    raise ValueError(
                        "Wiki ACL apply requires a lifecycle/event projection committer"
                    )
                manifest_path, projection_receipt = self._apply_plan(
                    plan,
                    backup_dir,
                )
                report["changed"] = len(plan)
                report["backup_files"] = len(plan)
                report["backup_manifest"] = str(manifest_path)
                if projection_receipt is not None:
                    report["wiki_projection"] = asdict(projection_receipt)
            else:
                report["backup_files"] = 0
                report["backup_manifest"] = ""
        return report

    def _validate_backup_dir(
        self,
        backup_dir: Path,
        targets: Sequence[str],
    ) -> None:
        if backup_dir.exists() and not backup_dir.is_dir():
            raise ValueError("ACL backup path exists and is not a directory")
        if backup_dir.exists() and any(backup_dir.iterdir()):
            raise ValueError("ACL backup directory must not exist or must be empty")
        backup_resolved = backup_dir.resolve()
        roots = {"wiki": self.wiki_dir, "raw": self.raw_dir}
        for kind in targets:
            root = roots[kind].resolve()
            if (
                backup_resolved == root
                or backup_resolved in root.parents
                or root in backup_resolved.parents
            ):
                raise ValueError(
                    f"ACL backup directory must be disjoint from the {kind} source root"
                )

    def _canonical_raw_provenance(self) -> Dict[tuple[str, str], set[str]]:
        from core.frontmatter import fm_get, read_strict_frontmatter_document

        provenance: Dict[tuple[str, str], set[str]] = {}
        if not self.raw_dir.exists():
            return provenance
        for path in self.raw_dir.rglob("*.md"):
            try:
                frontmatter, _, _ = read_strict_frontmatter_document(
                    path,
                    errors="strict",
                )
            except (UnicodeError, ValueError):
                continue
            source_agent = _lower(
                fm_get(frontmatter, "source_agent") or fm_get(frontmatter, "source")
            )
            session_id = str(
                fm_get(frontmatter, "session_id") or fm_get(frontmatter, "source_session") or ""
            ).strip()
            if source_agent in _KNOWN_SOURCE_AGENTS and session_id:
                key = (source_agent, session_id)
                provenance.setdefault(key, set()).add(_lower(fm_get(frontmatter, "project")))
        return provenance

    def _plan_file(
        self,
        path: Path,
        *,
        kind: str,
        canonical_raw_provenance: Dict[tuple[str, str], set[str]],
    ) -> tuple[str, str | None]:
        from core.frontmatter import (
            fm_get,
            read_strict_frontmatter_document,
            write_frontmatter,
        )

        try:
            frontmatter, body, content = read_strict_frontmatter_document(
                path,
                errors="strict",
            )
        except (UnicodeError, ValueError):
            return "parse_error", None

        source_agent = _lower(fm_get(frontmatter, "source_agent") or fm_get(frontmatter, "source"))
        session_id = str(
            fm_get(frontmatter, "session_id") or fm_get(frontmatter, "source_session") or ""
        ).strip()
        project = str(fm_get(frontmatter, "project") or "").strip()
        has_identity = source_agent in _KNOWN_SOURCE_AGENTS and bool(session_id)
        existing_scope = _lower(fm_get(frontmatter, "scope"))
        existing_status = _lower(fm_get(frontmatter, "acl_reconciliation_status"))
        existing_schema = int(fm_get(frontmatter, "acl_schema_version") or 0)
        existing_complete = fm_get(frontmatter, "acl_metadata_complete") is True
        raw_projects = canonical_raw_provenance.get(
            (source_agent, session_id),
            set(),
        )
        desired_scope = ""
        preserve_server_principal = (
            kind == "wiki"
            and existing_status == "server_principal"
            and existing_schema == 1
            and existing_complete
            and source_agent in _KNOWN_SOURCE_AGENTS
            and existing_scope in _ACL_SCOPES
            and existing_scope != "restricted"
            and (existing_scope != "private" or bool(session_id))
            and (existing_scope != "project" or bool(project))
        )
        if kind == "raw" and has_identity:
            proven = True
            desired_scope = "private"
            desired_status = "proven"
        elif preserve_server_principal:
            proven = True
            desired_scope = existing_scope
            desired_status = "server_principal"
        elif has_identity and raw_projects:
            proven = True
            desired_scope = "private"
            desired_status = "proven"
        else:
            proven = False
        if proven:
            desired: Dict[str, Any] = {
                "scope": desired_scope,
                "source_agent": source_agent,
                "acl_schema_version": 1,
                "acl_metadata_complete": True,
                "acl_reconciliation_status": desired_status,
            }
            if session_id:
                desired["session_id"] = session_id
            if project:
                desired["project"] = project
            category = "proven"
        else:
            desired = {
                "scope": "restricted",
                "acl_schema_version": 1,
                "acl_metadata_complete": True,
                "acl_reconciliation_status": "restricted_unknown",
            }
            category = "restricted"

        if all(frontmatter.get(key) == value for key, value in desired.items()):
            return category, None

        if not _ACL_KEYS.intersection(frontmatter):
            rendered = self._render_acl_fields(content, desired)
            if rendered is None:
                return "parse_error", None
        else:
            updated = dict(frontmatter)
            updated.update(desired)
            rendered = write_frontmatter(updated, body)
        return f"{category}:change", rendered

    @staticmethod
    def _render_acl_fields(
        content: str,
        desired: Mapping[str, Any],
    ) -> str | None:
        import yaml

        closing = content.find("\n---", 3)
        if closing < 0:
            return None
        block = yaml.safe_dump(
            dict(desired),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).rstrip()
        prefix = content[:closing].rstrip("\n")
        suffix = content[closing:]
        return f"{prefix}\n{block}{suffix}"

    def _apply_plan(
        self,
        plan: Sequence[_ACLPlanItem],
        backup_dir: Path,
    ) -> tuple[Path, WikiProjectionBatchReceipt | None]:
        """Back up the full batch, then replace files or restore every attempt."""

        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        backup_paths: Dict[Path, Path] = {}
        manifest_files: List[Dict[str, str]] = []
        for item in plan:
            if self._sha256_path(item.path) != item.original_sha256:
                raise RuntimeError(f"ACL source changed while planning: {item.path}")
            relative_path = item.path.relative_to(item.root)
            backup_path = backup_dir / item.kind / relative_path
            if backup_path.exists():
                raise FileExistsError(f"ACL backup already exists: {backup_path}")
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.path, backup_path)
            if self._sha256_path(backup_path) != item.original_sha256:
                raise RuntimeError(f"ACL backup hash mismatch: {backup_path}")
            backup_paths[item.path] = backup_path
            manifest_files.append(
                {
                    "kind": item.kind,
                    "relative_path": relative_path.as_posix(),
                    "original_sha256": item.original_sha256,
                    "desired_sha256": item.desired_sha256,
                }
            )

        for item in plan:
            if self._sha256_path(item.path) != item.original_sha256:
                raise RuntimeError(f"ACL source changed during backup: {item.path}")

        manifest_path = backup_dir / "acl-reconciliation-manifest.json"
        manifest: dict[str, Any] = {
            "schema_version": "mnemos.acl_reconciliation_backup.v1",
            "status": "prepared",
            "files": manifest_files,
        }
        self._atomic_write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

        attempted: List[_ACLPlanItem] = []
        projection_receipt: WikiProjectionBatchReceipt | None = None
        try:
            for item in plan:
                if self._sha256_path(item.path) != item.original_sha256:
                    raise RuntimeError(f"ACL source changed before replace: {item.path}")
                attempted.append(item)
                self._atomic_write_text(item.path, item.content)
                if self._sha256_path(item.path) != item.desired_sha256:
                    raise RuntimeError(f"ACL source verification failed: {item.path}")
            wiki_change_count = sum(item.kind == "wiki" for item in plan)
            if wiki_change_count:
                assert self._wiki_projection_commit is not None
                projection_receipt = self._wiki_projection_commit(
                    backup_dir,
                    wiki_change_count,
                )
        except BaseException as exc:
            rollback_errors: List[str] = []
            for item in reversed(attempted):
                try:
                    backup_path = backup_paths[item.path]
                    self._atomic_write_text(
                        item.path,
                        # Decode raw backup bytes directly: Path.read_text uses
                        # universal-newline translation and would turn CRLF
                        # preimages into LF during rollback.
                        read_bytes_value(backup_path).decode("utf-8", errors="strict"),
                    )
                    if self._sha256_path(item.path) != item.original_sha256:
                        raise RuntimeError("restored source hash mismatch")
                except BaseException as rollback_exc:
                    rollback_errors.append(f"{item.path}: {rollback_exc}")
            manifest["status"] = "rollback_failed" if rollback_errors else "rolled_back"
            manifest["failure"] = f"{type(exc).__name__}: {exc}"
            manifest["rollback_errors"] = rollback_errors
            try:
                self._atomic_write_text(
                    manifest_path,
                    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                )
            except OSError:
                pass
            detail = (
                "; ".join(rollback_errors) if rollback_errors else "all attempted files restored"
            )
            raise RuntimeError(f"ACL batch apply failed; {detail}") from exc

        manifest["status"] = "committed"
        manifest["wiki_projection"] = (
            asdict(projection_receipt) if projection_receipt is not None else None
        )
        self._atomic_write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return manifest_path, projection_receipt

    @staticmethod
    def _sha256_bytes(payload: bytes) -> str:
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    @classmethod
    def _sha256_path(cls, path: Path) -> str:
        return cls._sha256_bytes(read_bytes_value(path))

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        mode = path.stat().st_mode if path.exists() else 0o600
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.acl-",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            os.chmod(temporary_path, mode)
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


MCP_TOOL_POLICIES: Dict[str, str] = {
    "wiki_search": "memory_read",
    "wiki_read": "memory_read",
    "wiki_write": "memory_write",
    "session_search": "memory_read",
    "capture_turn": "capture_write",
    "capture_session": "capture_write",
    "end_session": "capture_write",
    "capture_status": "public_metadata",
    "session_save": "capture_write",
    "knowledge_ingest": "memory_write",
    "knowledge_distill": "memory_write",
    "document_process": "admin_runtime",
    "wiki_build": "admin_runtime",
    "memory_write_project": "memory_write",
    "memory_write_framework": "memory_write",
    "memory_write_global": "memory_write",
    "memory_search": "memory_read",
    "preflight_inject": "memory_read",
    "guard_check": "memory_read",
    "persona_summary": "memory_read",
    "persona_behavior_prompt": "memory_read",
    "persona_behavior_metrics": "memory_read",
    "persona_record_explicit_evidence": "memory_write",
    "persona_update": "admin_runtime",
    "signal_collect": "admin_runtime",
    "retrospective_list": "memory_read",
    "check_pending_recaps": "memory_read",
    "recap_start": "feedback_write",
    "recap_submit": "feedback_write",
    "recap_finalize": "feedback_write",
    "recap_skip": "feedback_write",
    "recap_feedback": "feedback_write",
    "recap_status": "memory_read",
    "recap_claim_owner": "feedback_write",
    "knowledge_source_list": "memory_read",
    "health_check": "public_metadata",
    "agent_runtime_probe": "capture_write",
    "self_diagnose": "public_metadata",
    "configure_wiki": "admin_runtime",
    "detect_sources": "public_metadata",
    "context_aware_search": "memory_read",
    "build_cognitive_state": "memory_read",
    "record_decision": "memory_write",
    "apply_outcome": "memory_write",
    "intent_route": "memory_read",
    "intent_correct": "feedback_write",
    "blindspot_check": "memory_read",
    "predictive_push": "memory_read",
    "delivery_display_ack": "feedback_write",
    "push_feedback": "feedback_write",
    "freshness_check": "memory_read",
    "observation_run": "admin_runtime",
    "observation_search": "memory_read",
    "reflect_on_input": "memory_write",
    "reflect_manually": "memory_write",
    "reflection_feedback": "feedback_write",
    "reflection_pending": "memory_read",
}

_CALLER_IDENTITY_ARGUMENTS = frozenset(
    {
        "agent",
        "allow_cross_agent",
        "authorized_agents",
        "source_agent",
        "source_agents",
        "owner_agent",
    }
)
_PRINCIPAL_BOUND_SOURCE_TOOLS = frozenset(
    {"capture_turn", "capture_session", "end_session", "capture_status", "session_save"}
)
_CALLER_ACL_ARGUMENTS = frozenset(
    {
        "agent",
        "allow_cross_agent",
        "authorized_agents",
        "source",
        "source_agent",
        "source_session",
        "session_id",
        "acl_schema_version",
        "acl_metadata_complete",
        "acl_reconciliation_status",
    }
)
_ACL_TAG_KEYS = _ACL_KEYS.union({"agent", "session", "source", "source_session"})


def _contains_acl_tag_override(arguments: Mapping[str, Any]) -> bool:
    tags = arguments.get("tags")
    if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes)):
        return False
    for raw_tag in tags:
        tag = str(raw_tag or "").strip()
        for separator in ("=", ":"):
            if separator in tag:
                key, _value = tag.split(separator, 1)
                if key.strip().lower() in _ACL_TAG_KEYS:
                    return True
                break
    return False


def authorize_tool_call(
    principal: PrincipalEnvelope,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> AuthorizedToolCall:
    """Authorize and sanitize a tool call before its handler executes."""
    policy = MCP_TOOL_POLICIES.get(str(tool_name or ""), "")
    if not policy:
        return AuthorizedToolCall(False, "tool_policy_missing")
    if _CALLER_IDENTITY_ARGUMENTS.intersection(arguments):
        return AuthorizedToolCall(False, "caller_identity_override_forbidden", policy=policy)
    if tool_name in _PRINCIPAL_BOUND_SOURCE_TOOLS and "source_agent" in arguments:
        return AuthorizedToolCall(False, "caller_identity_override_forbidden", policy=policy)
    frontmatter = arguments.get("frontmatter")
    if isinstance(frontmatter, Mapping) and _CALLER_ACL_ARGUMENTS.intersection(frontmatter):
        return AuthorizedToolCall(False, "caller_acl_override_forbidden", policy=policy)
    if isinstance(frontmatter, Mapping) and _contains_acl_tag_override(frontmatter):
        return AuthorizedToolCall(False, "caller_acl_override_forbidden", policy=policy)
    if tool_name == "knowledge_ingest" and _contains_acl_tag_override(arguments):
        return AuthorizedToolCall(False, "caller_acl_override_forbidden", policy=policy)
    if policy not in principal.capabilities:
        return AuthorizedToolCall(False, "principal_capability_missing", policy=policy)
    requested_project = _lower(arguments.get("project"))
    if not requested_project and isinstance(frontmatter, Mapping):
        requested_project = _lower(frontmatter.get("project"))
    requested_scope = _lower(frontmatter.get("scope")) if isinstance(frontmatter, Mapping) else ""
    if (
        tool_name == "wiki_write"
        and requested_scope == "private"
        and not _lower(arguments.get("session_id"))
    ):
        return AuthorizedToolCall(
            False,
            "private_scope_session_required",
            policy=policy,
        )
    if tool_name == "wiki_write" and requested_scope == "project" and not requested_project:
        return AuthorizedToolCall(
            False,
            "project_scope_project_required",
            policy=policy,
        )
    allowed_projects = {_lower(project) for project in principal.allowed_projects}
    if (
        requested_project
        and "*" not in allowed_projects
        and requested_project not in allowed_projects
    ):
        return AuthorizedToolCall(
            False,
            "principal_project_grant_missing",
            policy=policy,
        )
    return AuthorizedToolCall(
        True,
        "authorized",
        arguments=dict(arguments),
        policy=policy,
    )


def bind_write_acl(
    principal: PrincipalEnvelope,
    frontmatter: Mapping[str, Any] | None,
    *,
    default_scope: str = "agent",
    project: str = "",
    session_id: str = "",
    page_path: str = "",
) -> Dict[str, Any]:
    """Bind write provenance to an immutable server principal."""
    bound = dict(frontmatter or {})
    scope = _lower(bound.get("scope") or default_scope)
    if scope not in {"agent", "private", "project", "framework", "global"}:
        scope = default_scope
    path_scope, path_project = _resolve_scope_from_page_id(
        str(page_path or "").replace("\\", "/").removesuffix(".md"),
        "",
    )
    if path_scope and bound.get("scope") and scope != path_scope:
        raise ValueError("acl_path_scope_conflict")
    if path_scope:
        scope = path_scope
    raw_project = str(project or bound.get("project") or path_project or "").strip()
    item_project = _lower(raw_project)
    if (
        item_project
        and item_project not in {_lower(value) for value in principal.allowed_projects}
        and "*" not in {_lower(value) for value in principal.allowed_projects}
    ):
        raise ValueError("principal_project_grant_missing")
    if scope == "project" and not item_project:
        raise ValueError("project_scope_project_required")
    raw_session_id = str(session_id or "").strip()
    if scope == "private" and not raw_session_id:
        raise ValueError("private_scope_session_required")
    bound.update(
        {
            "scope": scope,
            "source_agent": principal.agent,
            "acl_schema_version": 1,
            "acl_metadata_complete": True,
            "acl_reconciliation_status": "server_principal",
        }
    )
    if scope == "private":
        bound["session_id"] = raw_session_id
    else:
        bound.pop("session_id", None)
    if item_project:
        bound["project"] = raw_project
    else:
        bound.pop("project", None)
    return bound


def complete_write_acl(
    frontmatter: Mapping[str, Any] | None,
    *,
    principal: PrincipalEnvelope | None = None,
    default_scope: str = "agent",
    project: str = "",
    session_id: str = "",
    page_path: str = "",
) -> Dict[str, Any]:
    """Ensure every new Wiki write has a complete, fail-closed ACL envelope."""
    if principal is not None:
        return bind_write_acl(
            principal,
            frontmatter,
            default_scope=default_scope,
            project=project,
            session_id=session_id,
            page_path=page_path,
        )
    from core.frontmatter import fm_get

    completed = dict(frontmatter or {})
    if (
        completed.get("acl_metadata_complete") is True
        and int(completed.get("acl_schema_version") or 0) == 1
        and completed.get("scope")
    ):
        return completed
    source_agent = _lower(fm_get(completed, "source_agent") or fm_get(completed, "source"))
    session_id = str(
        fm_get(completed, "session_id") or fm_get(completed, "source_session") or ""
    ).strip()
    if source_agent in _KNOWN_SOURCE_AGENTS and session_id:
        completed.update(
            {
                "scope": _lower(completed.get("scope") or default_scope),
                "source_agent": source_agent,
                "session_id": session_id,
                "acl_schema_version": 1,
                "acl_metadata_complete": True,
                "acl_reconciliation_status": "provenance_write",
            }
        )
        if project:
            completed["project"] = _lower(project)
        return completed
    completed.update(
        {
            "scope": "restricted",
            "acl_schema_version": 1,
            "acl_metadata_complete": True,
            "acl_reconciliation_status": "restricted_unknown",
        }
    )
    completed.pop("source_agent", None)
    completed.pop("session_id", None)
    return completed


def authorize_item(
    principal: PrincipalEnvelope,
    item: Mapping[str, Any],
    narrowing: AccessNarrowing,
) -> AccessDecision:
    """Authorize an item using only server grants plus request narrowing."""
    if "memory_read" not in principal.capabilities and "*" not in principal.capabilities:
        return AccessDecision(False, "principal_capability_missing")
    envelope_decision = validate_acl_envelope(item)
    if not envelope_decision.allowed:
        return envelope_decision
    requested_project = _lower(narrowing.project)
    allowed_projects = {_lower(project) for project in principal.allowed_projects}
    if (
        requested_project
        and "*" not in allowed_projects
        and requested_project not in allowed_projects
    ):
        return AccessDecision(False, "principal_project_grant_missing")
    return can_read_item(
        item,
        AccessContext(
            agent=principal.agent,
            session_id=str(narrowing.session_id or ""),
            project=requested_project,
            allow_cross_agent=bool(principal.allowed_source_agents),
            authorized_agents=principal.allowed_source_agents,
        ),
    )


def validate_acl_envelope(item: Mapping[str, Any]) -> AccessDecision:
    """Validate one serialized ACL envelope before policy evaluation."""
    metadata = _item_metadata(item)
    tags = parse_tags(_extract_tags(item, metadata))

    def values_for(key: str, *tag_keys: str) -> set[str]:
        values = {
            _lower(container.get(key))
            for container in (item, metadata)
            if container.get(key) not in (None, "")
        }
        if key == "source_agent":
            values.update(
                _lower(container.get("source"))
                for container in (item, metadata)
                if container.get("source") not in (None, "")
            )
        elif key == "session_id":
            values.update(
                _lower(container.get("source_session"))
                for container in (item, metadata)
                if container.get("source_session") not in (None, "")
            )
        values.update(
            _lower(tags.get(tag_key)) for tag_key in tag_keys if tags.get(tag_key) not in (None, "")
        )
        return {value for value in values if value}

    for key, tag_keys in (
        ("scope", ("scope",)),
        ("source_agent", ("source", "agent")),
        ("session_id", ("session", "session_id")),
        ("project", ("project",)),
    ):
        if len(values_for(key, *tag_keys)) > 1:
            return AccessDecision(False, "acl_metadata_conflict")

    completeness_values = {
        container.get("acl_metadata_complete")
        for container in (item, metadata)
        if "acl_metadata_complete" in container
    }
    if completeness_values != {True}:
        return AccessDecision(False, "acl_metadata_missing")

    schema_values = {
        container.get("acl_schema_version")
        for container in (item, metadata)
        if "acl_schema_version" in container
    }
    if schema_values != {1}:
        return AccessDecision(False, "acl_schema_unsupported")

    status_values = values_for("acl_reconciliation_status")
    if len(status_values) != 1:
        return AccessDecision(False, "acl_reconciliation_status_missing")
    status = next(iter(status_values))
    if status == "restricted_unknown":
        return AccessDecision(False, "acl_reconciliation_required")
    if status not in _TRUSTED_ACL_STATUSES:
        return AccessDecision(False, "acl_reconciliation_status_invalid")

    scope_values = values_for("scope", "scope")
    if len(scope_values) != 1:
        return AccessDecision(False, "acl_scope_missing")
    scope = next(iter(scope_values))
    if scope not in _ACL_SCOPES or scope == "restricted":
        return AccessDecision(False, "acl_reconciliation_required")

    page_id = _get_value(item, "page_id", "page_path", "path")
    path_scope, _ = _resolve_scope_from_page_id(page_id, "")
    if path_scope and path_scope != scope:
        return AccessDecision(False, "acl_metadata_conflict")

    source_values = values_for("source_agent", "source", "agent")
    if len(source_values) != 1:
        return AccessDecision(False, "acl_provenance_missing")
    if scope == "private" and len(values_for("session_id", "session", "session_id")) != 1:
        return AccessDecision(False, "acl_private_session_missing")
    if scope == "project" and len(values_for("project", "project")) != 1:
        return AccessDecision(False, "acl_project_missing")
    return AccessDecision(True, "acl_envelope_valid")


def parse_tags(tags: Iterable[str]) -> Dict[str, str]:
    """Parse ``key=value`` and ``key:value`` tags into a normalized dict."""
    parsed: Dict[str, str] = {}
    for raw in tags or []:
        tag = str(raw).strip()
        if not tag:
            continue
        if "=" in tag:
            key, value = tag.split("=", 1)
        elif ":" in tag:
            key, value = tag.split(":", 1)
        else:
            continue
        parsed[key.strip().lower()] = value.strip()
    return parsed


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _get_value(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _item_metadata(item: Mapping[str, Any]) -> Dict[str, Any]:
    metadata = item.get("metadata") or item.get("frontmatter") or {}
    return metadata if isinstance(metadata, dict) else {}


def _extract_tags(item: Mapping[str, Any], metadata: Mapping[str, Any]) -> List[str]:
    """从 item 与 metadata 中提取原始 tag 字符串。"""
    tag_values: List[str] = []
    for source in (item.get("tags"), metadata.get("tags")):
        if isinstance(source, str):
            tag_values.extend(part.strip() for part in source.split(","))
        elif isinstance(source, Sequence):
            tag_values.extend(str(part) for part in source)
    return tag_values


def _resolve_scope(
    item: Mapping[str, Any], metadata: Mapping[str, Any], parsed: Dict[str, str]
) -> str:
    """解析 scope 字段。"""
    return (
        _get_value(item, "scope")
        or str(metadata.get("scope") or "")
        or parsed.get("scope")
        or "restricted"
    )


def _resolve_source_agent(
    item: Mapping[str, Any], metadata: Mapping[str, Any], parsed: Dict[str, str]
) -> str:
    """解析 source_agent 字段。"""
    return (
        _get_value(item, "source_agent", "source")
        or str(metadata.get("source_agent") or metadata.get("source") or "")
        or parsed.get("source", "")
        or parsed.get("agent", "")
    )


def _resolve_session_id(
    item: Mapping[str, Any], metadata: Mapping[str, Any], parsed: Dict[str, str]
) -> str:
    """解析 session_id 字段。"""
    return (
        _get_value(item, "session_id")
        or str(metadata.get("session_id") or "")
        or parsed.get("session", "")
        or parsed.get("session_id", "")
    )


def _resolve_project(
    item: Mapping[str, Any], metadata: Mapping[str, Any], parsed: Dict[str, str]
) -> str:
    """解析 project 字段。"""
    return (
        _get_value(item, "project")
        or str(metadata.get("project") or "")
        or parsed.get("project", "")
    )


def _resolve_scope_from_page_id(page_id: str, project: str) -> Tuple[Optional[str], str]:
    """根据 page_id 路径前缀覆盖 scope 并推断 project。"""
    if page_id.startswith("scopes/project/"):
        parts = page_id.split("/")
        if len(parts) >= 3 and not project:
            project = parts[2]
        return "project", project
    if page_id.startswith("scopes/framework/"):
        return "framework", project
    if page_id.startswith("scopes/global/"):
        return "global", project
    return None, project


def item_access_fields(item: Mapping[str, Any]) -> Dict[str, str]:
    """Extract policy fields from a search result without reading page content."""
    metadata = _item_metadata(item)
    tag_values = _extract_tags(item, metadata)
    parsed = parse_tags(tag_values)

    page_id = _get_value(item, "page_id", "page_path", "path")
    scope = _resolve_scope(item, metadata, parsed)
    source_agent = _resolve_source_agent(item, metadata, parsed)
    session_id = _resolve_session_id(item, metadata, parsed)
    project = _resolve_project(item, metadata, parsed)

    page_scope, project = _resolve_scope_from_page_id(page_id, project)
    if page_scope:
        scope = page_scope

    return {
        "scope": _lower(scope or "restricted"),
        "source_agent": _lower(source_agent),
        "session_id": str(session_id or ""),
        "project": _lower(project),
        "page_id": page_id,
    }


def _check_missing_identity(
    caller_agent: str,
    scope: str,
    source_agent: str,
    item_session: str,
    item_project: str,
) -> Optional[AccessDecision]:
    """缺少 caller agent 身份时拒绝受限项。"""
    if not caller_agent:
        if scope in _RESTRICTED_SCOPES or source_agent or item_session or item_project:
            return AccessDecision(False, "missing_agent_identity")
    return None


def _check_private_scope(
    scope: str,
    source_agent: str,
    caller_agent: str,
    same_agent: bool,
    item_session: str,
    context: AccessContext,
) -> Optional[AccessDecision]:
    """校验 private scope 的读取权限。"""
    if scope != "private":
        return None
    if source_agent and caller_agent and not same_agent:
        return AccessDecision(False, "private_cross_agent_denied")
    if item_session and not context.session_id:
        return AccessDecision(False, "private_session_requires_context")
    if item_session and context.session_id and item_session != context.session_id:
        return AccessDecision(False, "private_session_mismatch")
    return AccessDecision(True, "private_same_agent_or_unscoped")


def _check_cross_agent(
    source_agent: str,
    caller_agent: str,
    same_agent: bool,
    context: AccessContext,
) -> Optional[AccessDecision]:
    """校验跨 agent 读取授权。"""
    if not (source_agent and caller_agent and not same_agent):
        return None
    if context.allow_cross_agent:
        allowed_agents = {_lower(agent) for agent in context.authorized_agents}
        if source_agent in allowed_agents:
            return AccessDecision(True, "cross_agent_authorized")
    return AccessDecision(False, "cross_agent_requires_authorization")


def _check_project_scope(
    scope: str, item_project: str, context: AccessContext
) -> Optional[AccessDecision]:
    """校验 project scope 的读取权限。"""
    if scope != "project":
        return None
    caller_project = _lower(context.project)
    if item_project and not caller_project:
        return AccessDecision(False, "project_scope_requires_context")
    if item_project and caller_project and item_project != caller_project:
        return AccessDecision(False, "project_scope_mismatch")
    return None


def can_read_item(item: Mapping[str, Any], context: AccessContext) -> AccessDecision:
    """Return whether ``context`` may read ``item``."""
    fields = item_access_fields(item)
    scope = fields["scope"]
    source_agent = fields["source_agent"]
    caller_agent = _lower(context.agent)
    same_agent = bool(source_agent and caller_agent and source_agent == caller_agent)
    item_session = fields["session_id"]
    item_project = fields["project"]

    if scope == "restricted":
        return AccessDecision(False, "acl_reconciliation_required")

    decision = _check_missing_identity(
        caller_agent, scope, source_agent, item_session, item_project
    )
    if decision is not None:
        return decision

    decision = _check_private_scope(
        scope, source_agent, caller_agent, same_agent, item_session, context
    )
    if decision is not None:
        return decision

    decision = _check_cross_agent(
        source_agent,
        caller_agent,
        same_agent,
        context,
    )
    if decision is not None:
        return decision

    decision = _check_project_scope(scope, item_project, context)
    if decision is not None:
        return decision

    return AccessDecision(True, "allowed")


def filter_readable_items(
    items: Iterable[Mapping[str, Any]],
    context: AccessContext,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Filter items and return a compact reason summary."""
    readable: List[Dict[str, Any]] = []
    summary: Dict[str, int] = {}
    for item in items:
        decision = can_read_item(item, context)
        summary[decision.reason] = summary.get(decision.reason, 0) + 1
        if decision.allowed:
            copy = dict(item)
            copy.setdefault("access_reason", decision.reason)
            readable.append(copy)
    return readable, summary


def filter_authorized_items(
    items: Iterable[Mapping[str, Any]],
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Strict MCP filtering with server principal and complete ACL metadata."""
    readable: List[Dict[str, Any]] = []
    summary: Dict[str, int] = {}
    for item in items:
        decision = authorize_item(principal, item, narrowing)
        summary[decision.reason] = summary.get(decision.reason, 0) + 1
        if decision.allowed:
            copy = dict(item)
            copy.setdefault("access_reason", decision.reason)
            readable.append(copy)
    return readable, summary
