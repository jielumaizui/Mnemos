"""Privacy deletion owner for formal Wiki projections.

The Wiki lifecycle ledger remains the only owner of formal page mutations.
This adapter only selects ACL-authorized pages from frontmatter, creates a
content-free subject-deletion receipt, and drives the existing lifecycle
delete/publish path.  It deliberately refuses unregistered or ACL-ambiguous
legacy pages instead of treating a Markdown file deletion as verified.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from core.access_policy import validate_acl_envelope
from core.frontmatter import normalize_frontmatter, read_frontmatter_only
from core.kia.relation_endpoint_quality import is_derived_kg_scan_path
from core.trust.models import sha256_text
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
    TrustedVaultMutationResult,
    TrustedVaultMutationService,
    commit_trusted_markdown_delete,
    trusted_markdown_material_action_binding,
)
from core.wiki_projection_lifecycle import WikiProjectionLedger
from core.wiki_projection_publisher import publish_wiki_mutation

if TYPE_CHECKING:
    from core.cognitive.decision_trace import (
        MaterialActionAuthorization,
        MaterialActionRequest,
    )


WIKI_SUBJECT_DELETION_SCHEMA_VERSION = "mnemos.wiki_subject_deletion.v1"
_SUPPORTED_SCOPES = frozenset(
    {"all", "agent", "session", "project", "path", "source", "wiki_page", "raw_event_id"}
)


def subject_scope_hash(scope_kind: str, scope_value: str) -> str:
    """Hash the ownership selector before it enters any durable receipt."""

    payload = f"{str(scope_kind).strip().lower()}:{str(scope_value).strip()}"
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_vault_markdown(path: Path, vault: Path) -> bool:
    """Keep hidden/system projection artifacts outside subject page selection."""

    try:
        relative = path.relative_to(vault)
    except ValueError:
        return False
    return path.suffix.lower() == ".md" and not any(
        part.startswith(".") for part in relative.parts
    )


def _normalized_strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


class WikiSubjectDeletionService:
    """Apply a confirmed subject deletion to ledger-owned Wiki pages only."""

    def __init__(
        self,
        *,
        wiki_dir: Path | str,
        projection_db_path: Path | str,
        event_bus: Any | None = None,
        material_action_resolver: Callable[
            [MaterialActionRequest, Mapping[str, Any]],
            MaterialActionAuthorization,
        ]
        | None = None,
    ):
        self.wiki_dir = Path(wiki_dir).expanduser().resolve(strict=False)
        self.projection_db_path = Path(projection_db_path).expanduser()
        self.event_bus = event_bus
        self.material_action_resolver = material_action_resolver
        if not self.projection_db_path.is_file():
            raise FileNotFoundError(
                "Wiki subject deletion requires an initialized projection ledger"
            )
        self.ledger = WikiProjectionLedger(self.projection_db_path)

    def _relative_path(self, path: Path) -> str:
        return path.resolve(strict=False).relative_to(self.wiki_dir).as_posix()

    @staticmethod
    def _matches_subject(
        frontmatter: Mapping[str, Any],
        *,
        relative_path: str,
        scope_kind: str,
        scope_value: str,
    ) -> bool:
        """Compare an ownership request against header-only, exact selectors."""

        value = str(scope_value).strip()
        value_lower = value.lower()
        if scope_kind == "all":
            return True
        if scope_kind == "agent":
            return str(frontmatter.get("source_agent") or "").strip().lower() == value_lower
        if scope_kind == "source":
            return value_lower in {
                str(frontmatter.get("source_agent") or "").strip().lower(),
                str(frontmatter.get("source") or "").strip().lower(),
            }
        if scope_kind == "session":
            return value in {
                str(frontmatter.get("session_id") or "").strip(),
                str(frontmatter.get("source_session") or "").strip(),
            }
        if scope_kind == "project":
            return str(frontmatter.get("project") or "").strip().lower() == value_lower
        if scope_kind in {"path", "wiki_page"}:
            normalized = relative_path.replace("\\", "/")
            requested = value.replace("\\", "/")
            return requested in {normalized, "/" + normalized}
        if scope_kind == "raw_event_id":
            return value in _normalized_strings(frontmatter.get("source_event_ids"))
        return False

    def _authorized_targets(
        self,
        *,
        scope_kind: str,
        scope_value: str,
    ) -> tuple[list[Path], dict[str, int]]:
        """Build a delete target list before any page body is opened."""

        targets: list[Path] = []
        summary = {
            "scanned_page_count": 0,
            "acl_unknown_count": 0,
            "matched_header_count": 0,
            "derived_projection_page_count": 0,
        }
        if not self.wiki_dir.is_dir():
            return targets, summary
        for candidate in sorted(self.wiki_dir.rglob("*.md")):
            if candidate.is_symlink() or not _is_vault_markdown(candidate, self.wiki_dir):
                continue
            summary["scanned_page_count"] += 1
            try:
                resolved = candidate.resolve(strict=True)
                relative_path = self._relative_path(resolved)
                if is_derived_kg_scan_path(resolved, self.wiki_dir):
                    # These pages are output projections.  Their required
                    # consumer owns reference removal and must write a
                    # terminal receipt for the source deletion mutation;
                    # treating them as independent source targets would both
                    # misclassify their ACL and bypass that propagation gate.
                    summary["derived_projection_page_count"] += 1
                    continue
                frontmatter = normalize_frontmatter(
                    read_frontmatter_only(resolved, errors="ignore")
                )
            except (OSError, ValueError):
                summary["acl_unknown_count"] += 1
                continue
            decision = validate_acl_envelope(
                {"page_path": relative_path, "frontmatter": frontmatter}
            )
            if not decision.allowed:
                summary["acl_unknown_count"] += 1
                continue
            if self._matches_subject(
                frontmatter,
                relative_path=relative_path,
                scope_kind=scope_kind,
                scope_value=scope_value,
            ):
                targets.append(resolved)
                summary["matched_header_count"] += 1
        return targets, summary

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _submit_trusted_mutation_delete_receipt(
        self,
        *,
        target: Path,
        deletion_receipt: Mapping[str, Any],
    ) -> TrustedVaultMutationResult:
        """Ask trusted push before the physical Markdown mutation happens."""

        content = target.read_text(encoding="utf-8")
        if self._content_hash(content) != str(deletion_receipt["before_content_sha256"]):
            raise RuntimeError("Wiki body changed after lifecycle identity was recorded")
        service = TrustedVaultMutationService(wiki_base=self.wiki_dir)
        from core.cognitive.decision_trace import MaterialActionRequest

        binding = trusted_markdown_material_action_binding(
            target_path=target,
            content="",
            proposed_action="data_subject_delete",
            expected_existing_hash=sha256_text(content),
        )
        request = MaterialActionRequest(
            owner=TRUSTED_MARKDOWN_OWNER,
            executor_id=TRUSTED_MARKDOWN_EXECUTOR,
            action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=str(
                self.projection_db_path.parent / "producer_consumer_ledger.db"
            ),
        )
        material_action = (
            self.material_action_resolver(request, deletion_receipt)
            if self.material_action_resolver is not None
            else None
        )
        return service.submit_markdown(
            target_path=target,
            content="",
            source="data_ownership_subject_delete",
            actor="data_ownership",
            evidence_refs=(
                f"wiki_subject_delete:{deletion_receipt['receipt_id']}",
                f"wiki_page:{deletion_receipt['page_id']}",
            ),
            proposed_action="data_subject_delete",
            expected_existing_hash=sha256_text(content),
            metadata={
                "operation": "data_subject_delete",
                "deletion_receipt_id": str(deletion_receipt["receipt_id"]),
                "page_id": str(deletion_receipt["page_id"]),
            },
            material_action=material_action,
        )

    def _delete_target(
        self,
        *,
        target: Path,
        request_id: str,
        scope_kind: str,
        scope_value_hash: str,
    ) -> dict[str, Any]:
        """Execute or resume one page deletion without exposing body bytes."""

        identity = self.ledger.page_identity(target)
        if identity is None:
            return {"status": "untracked"}
        if target.is_file():
            current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            if current_hash != identity["content_sha256"]:
                return {"status": "untracked_mutation"}
        try:
            receipt = self.ledger.prepare_subject_deletion(
                request_id=request_id,
                scope_kind=scope_kind,
                scope_value_hash=scope_value_hash,
                page_path=target,
            )
        except RuntimeError:
            # A different request owns the only receipt for this already
            # tombstoned page.  Do not invent a second deletion lineage.
            return {"status": "stalled_tombstone"}
        initial_status = str(receipt["status"])
        if initial_status == "proposed":
            return {"status": "proposal_pending"}
        if initial_status == "applied":
            mutation_id = str(receipt["mutation_id"])
            return {
                "status": "existing",
                "receipt_id": str(receipt["receipt_id"]),
                "mutation_id": mutation_id,
                "consumer_gaps": self.ledger.required_consumer_gaps(mutation_id),
                "physical_deleted": False,
            }

        mutation = None
        physical_deleted = False
        if initial_status == "planned":
            trusted = None
            if target.is_file():
                trusted = self._submit_trusted_mutation_delete_receipt(
                    target=target,
                    deletion_receipt=receipt,
                )
                if trusted.intercepted:
                    self.ledger.mark_subject_deletion_proposed(
                        str(receipt["receipt_id"]),
                        str(trusted.proposal_id),
                    )
                    return {"status": "proposal_pending"}
            mutation = self.ledger.record_mutation(target, mutation_type="delete")
            receipt = self.ledger.bind_subject_deletion_mutation(
                str(receipt["receipt_id"]),
                mutation.mutation_id,
            )
            if target.is_file():
                assert trusted is not None
                commit_trusted_markdown_delete(trusted, target_path=target)
                physical_deleted = True
        elif initial_status == "tombstoned":
            mutation = self.ledger.mutation_receipt(str(receipt["mutation_id"]))
            if mutation is None:
                raise RuntimeError("Wiki subject deletion receipt lost its tombstone mutation")
            if target.is_file():
                trusted = self._submit_trusted_mutation_delete_receipt(
                    target=target,
                    deletion_receipt=receipt,
                )
                if trusted.intercepted:
                    self.ledger.mark_subject_deletion_proposed(
                        str(receipt["receipt_id"]),
                        str(trusted.proposal_id),
                    )
                    return {"status": "proposal_pending_after_tombstone"}
                commit_trusted_markdown_delete(trusted, target_path=target)
                physical_deleted = True
        else:
            raise RuntimeError(f"unsupported Wiki subject deletion status: {initial_status}")

        assert mutation is not None
        published = publish_wiki_mutation(
            mutation,
            ledger=self.ledger,
            source="data_ownership_subject_delete",
            event_bus=self.event_bus,
        )
        applied = self.ledger.mark_subject_deletion_applied(
            str(receipt["receipt_id"]),
            event_trace_id=str(published["event_trace_id"]),
        )
        return {
            "status": "applied",
            "receipt_id": str(applied["receipt_id"]),
            "mutation_id": mutation.mutation_id,
            "consumer_gaps": self.ledger.required_consumer_gaps(mutation.mutation_id),
            "physical_deleted": physical_deleted,
        }

    def _resume_missing_target_receipt(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Resume propagation when a prior attempt removed the file first."""

        status = str(receipt.get("status") or "")
        mutation_id = str(receipt.get("mutation_id") or "")
        if status == "applied":
            return {
                "status": "existing",
                "receipt_id": str(receipt["receipt_id"]),
                "mutation_id": mutation_id,
                "consumer_gaps": self.ledger.required_consumer_gaps(mutation_id),
                "physical_deleted": False,
            }
        if status != "tombstoned" or not mutation_id:
            return {"status": "stalled_tombstone"}
        mutation = self.ledger.mutation_receipt(mutation_id)
        if mutation is None:
            return {"status": "failed"}
        published = publish_wiki_mutation(
            mutation,
            ledger=self.ledger,
            source="data_ownership_subject_delete",
            event_bus=self.event_bus,
        )
        applied = self.ledger.mark_subject_deletion_applied(
            str(receipt["receipt_id"]),
            event_trace_id=str(published["event_trace_id"]),
        )
        return {
            "status": "applied",
            "receipt_id": str(applied["receipt_id"]),
            "mutation_id": mutation_id,
            "consumer_gaps": self.ledger.required_consumer_gaps(mutation_id),
            "physical_deleted": False,
        }

    def delete_subject_scope(
        self,
        *,
        request_id: str,
        scope_kind: str,
        scope_value: str,
    ) -> dict[str, Any]:
        """Delete ACL-matched Wiki pages and report real propagation state.

        No nonterminal state is returned as ``verified``.  In particular, an
        EventBus publication is not enough: all lifecycle-required consumers
        must write a terminal receipt for the exact deletion mutation.
        """

        normalized_scope = str(scope_kind or "").strip().lower()
        if normalized_scope not in _SUPPORTED_SCOPES:
            return {
                "schema_version": WIKI_SUBJECT_DELETION_SCHEMA_VERSION,
                "status": "blocked",
                "target_count": 0,
                "verified": False,
                "error": "wiki_subject_deletion_scope_unsupported",
            }
        scope_value = str(scope_value)
        scope_value_hash = subject_scope_hash(normalized_scope, scope_value)
        targets, summary = self._authorized_targets(
            scope_kind=normalized_scope,
            scope_value=scope_value,
        )
        results: list[dict[str, Any]] = []
        seen_receipt_ids: set[str] = set()
        for target in targets:
            try:
                result = self._delete_target(
                    target=target,
                    request_id=request_id,
                    scope_kind=normalized_scope,
                    scope_value_hash=scope_value_hash,
                )
                results.append(result)
                if result.get("receipt_id"):
                    seen_receipt_ids.add(str(result["receipt_id"]))
            except (OSError, PermissionError, RuntimeError, ValueError, sqlite3.Error):
                results.append({"status": "failed"})

        # A physical deletion intentionally removes the Markdown file, so a
        # retry cannot rediscover it through a vault scan.  Its original typed
        # receipt is therefore the sole safe way to continue propagation.
        prior_receipts = self.ledger.subject_deletion_receipts_for_scope(
            scope_kind=normalized_scope,
            scope_value_hash=scope_value_hash,
        )
        active_target_paths = {
            str(target.resolve(strict=False))
            for target in targets
        }
        prior_missing_page_ids = {
            str(receipt["page_id"])
            for receipt in prior_receipts
            if str(receipt["page_path"]) not in active_target_paths
        }
        for receipt in prior_receipts:
            receipt_id = str(receipt["receipt_id"])
            if receipt_id in seen_receipt_ids:
                continue
            try:
                results.append(self._resume_missing_target_receipt(receipt))
            except (OSError, PermissionError, RuntimeError, ValueError, sqlite3.Error):
                results.append({"status": "failed"})

        statuses = [str(result.get("status") or "") for result in results]
        consumer_gap_count = sum(
            len(tuple(result.get("consumer_gaps") or ())) for result in results
        )
        blocked = bool(summary["acl_unknown_count"]) or any(
            status
            in {
                "failed",
                "untracked",
                "untracked_mutation",
                "stalled_tombstone",
                "proposal_pending",
                "proposal_pending_after_tombstone",
            }
            for status in statuses
        )
        applied_count = sum(status == "applied" for status in statuses)
        existing_count = sum(status == "existing" for status in statuses)
        physical_deleted_count = sum(
            result.get("physical_deleted") is True for result in results
        )
        if blocked:
            status = "blocked"
        elif applied_count:
            status = "applied"
        elif existing_count:
            status = "existing"
        else:
            status = "no_targets"
        verified = (
            status in {"applied", "existing", "no_targets"}
            and summary["acl_unknown_count"] == 0
            and consumer_gap_count == 0
        )
        return {
            "schema_version": WIKI_SUBJECT_DELETION_SCHEMA_VERSION,
            "status": status,
            "target_count": len(targets) + len(prior_missing_page_ids),
            "receipt_count": applied_count + existing_count,
            "physical_deleted_count": physical_deleted_count,
            "verified": verified,
            "pending_required_consumer_count": consumer_gap_count,
            **summary,
        }
