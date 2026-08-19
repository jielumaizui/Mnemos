"""Trusted-push bridge for application-layer formal writes."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.cognitive.decision_trace import (
    MaterialActionRequest,
    ProjectContractDecisionContext,
    ProjectContractMaterialActionResolver,
    build_exact_project_contract_evaluator,
)
from core.cognitive.state_contract import sha256_json
from core.trust.markdown_adapter import read_markdown_text
from core.trust.config import load_trusted_push_config
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
    TrustedVaultMutationResult,
    TrustedVaultMutationService,
    commit_trusted_markdown,
    trusted_markdown_material_action_binding,
)
from core.trust.models import sha256_text


APPLICATION_WIKI_DECISION_CONTRACT_ID = "project-contract:application-wiki-write"
APPLICATION_WIKI_DECISION_CONTRACT_REVISION = "mnemos.application_wiki_write.v1"
APPLICATION_WIKI_DECISION_CONTRACT_TEXT = (
    "An ACL-authorized application Wiki request may mutate only its validated "
    "vault path and exact rendered content through trusted push."
)
APPLICATION_WIKI_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.application.trusted_write_bridge",
        "producer": "write_application_wiki_page",
        "version": APPLICATION_WIKI_DECISION_CONTRACT_REVISION,
    }
)


def write_application_wiki_page(
    *,
    page_path: str,
    content: str,
    frontmatter: Mapping[str, Any] | None = None,
    logger: logging.Logger | None = None,
    principal: Any | None = None,
    session_id: str = "",
    project: str = "",
) -> dict[str, Any]:
    """Write or propose an application-layer Wiki page mutation."""
    from core.config import get_config

    log = logger or logging.getLogger(__name__)
    config = get_config()
    wiki_dir = config.wiki_dir

    safe_path = page_path.lstrip("/")
    if not safe_path or safe_path == ".":
        return {"success": False, "message": f"Wiki 页面路径不能为空: {page_path}"}
    target = (wiki_dir / safe_path).resolve()
    wiki_resolved = wiki_dir.resolve()

    path_error = _validate_wiki_target(target, wiki_resolved, page_path)
    if path_error:
        return path_error
    target.parent.mkdir(parents=True, exist_ok=True)

    from core.access_policy import complete_write_acl

    frontmatter_data = complete_write_acl(frontmatter)
    operation_created_at = datetime.now().astimezone().isoformat()
    frontmatter_data.setdefault("updated_at", operation_created_at)
    full_content = _render_frontmatter(frontmatter_data) + "\n\n" + content

    try:
        trusted_push = submit_application_wiki_write(
            wiki_dir=wiki_dir,
            target=target,
            content=full_content,
            page_path=safe_path,
            frontmatter_keys=frontmatter_data.keys(),
            operation_created_at=operation_created_at,
            principal=principal,
            session_id=session_id,
            project=project,
        )
        if trusted_push.intercepted:
            return {
                "success": True,
                "message": f"Wiki 页面已提交可信写入提案: {safe_path}",
                "path": safe_path,
                "size": len(full_content),
                "status": "proposed",
                "indexed": False,
                "trusted_push": trusted_push.to_dict(),
                "proposal_id": trusted_push.proposal_id,
            }

        commit_trusted_markdown(
            trusted_push,
            target_path=target,
            content=full_content,
        )
        return {
            "success": True,
            "message": f"Wiki 页面已写入: {safe_path}",
            "path": safe_path,
            "size": len(full_content),
            "status": "written",
            "indexed": True,
            "trusted_push": trusted_push.to_dict(),
        }
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        ImportError,
        AttributeError,
        sqlite3.Error,
    ) as exc:
        log.error("Wiki 写入失败: %s", exc, exc_info=True)
        return {"success": False, "message": f"写入失败: {exc}"}


def submit_application_wiki_write(
    *,
    wiki_dir: Path,
    target: Path,
    content: str,
    page_path: str,
    frontmatter_keys: Iterable[str],
    operation_created_at: str,
    principal: Any | None = None,
    session_id: str = "",
    project: str = "",
    evidence_refs: Iterable[str] | None = None,
    config: Any | None = None,
) -> TrustedVaultMutationResult:
    """Submit an application-level Wiki write to trusted push when enabled."""
    trusted_config = load_trusted_push_config(config, wiki_base=wiki_dir)
    service = TrustedVaultMutationService(wiki_base=wiki_dir, config=trusted_config)
    expected_existing_hash = sha256_text(read_markdown_text(target)) if target.is_file() else None
    binding = trusted_markdown_material_action_binding(
        target_path=target,
        content=content,
        proposed_action="application_wiki_write",
        expected_existing_hash=expected_existing_hash,
    )
    expected_request = MaterialActionRequest(
        owner=TRUSTED_MARKDOWN_OWNER,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
        action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
        expected_state_db=str(service.config.db_path.parent / "producer_consumer_ledger.db"),
    )
    principal_facts = {
        "principal_id": str(getattr(principal, "principal_id", "") or "system:application"),
        "agent": str(getattr(principal, "agent", "") or "mnemos"),
        "capability_id": str(getattr(principal, "capability_id", "") or "application"),
    }
    wiki_root = wiki_dir.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    try:
        relative_target = resolved_target.relative_to(wiki_root).as_posix()
    except ValueError:
        relative_target = ""
    source_facts = {
        "schema_version": "mnemos.application_wiki_write_facts.v1",
        "page_path": page_path,
        "content_hash": sha256_text(content),
        "frontmatter_keys": sorted(str(key) for key in frontmatter_keys),
        "session_id": str(session_id or ""),
        "project": str(project or ""),
        "principal": principal_facts,
        "expected_existing_hash": str(expected_existing_hash or ""),
        "evidence_refs": sorted(
            {str(ref).strip() for ref in (evidence_refs or ()) if str(ref).strip()}
        ),
    }
    source_facts_hash, evaluator = build_exact_project_contract_evaluator(
        expected_request=expected_request,
        source_facts=source_facts,
        decision_checks={
            "target_is_within_wiki": bool(relative_target),
            "page_path_matches_target": (relative_target == Path(page_path).as_posix().lstrip("/")),
            "content_is_bound": bool(content) and bool(source_facts["content_hash"]),
            "operation_time_is_bound": bool(str(operation_created_at).strip()),
        },
        approved_candidate_key="write_acl_authorized_application_page",
        approved_candidate_summary=(
            "Write the exact ACL-authorized application page through trusted push."
        ),
        rejected_candidate_key="reject_unbound_application_page",
        rejected_candidate_summary=(
            "Reject a page mutation outside the authorized application request."
        ),
        approved_reason_code="application_wiki_binding_verified",
        rejected_reason_code="application_wiki_binding_rejected",
        committed_metric="application_wiki_mutation_receipt",
        rejected_metric="unbound_application_wiki_mutation_count",
    )
    source_digest = source_facts_hash.split(":", 1)[1]
    resolver = ProjectContractMaterialActionResolver(
        ProjectContractDecisionContext(
            state_db_path=Path(expected_request.expected_state_db),
            contract_id=APPLICATION_WIKI_DECISION_CONTRACT_ID,
            contract_revision_id=APPLICATION_WIKI_DECISION_CONTRACT_REVISION,
            contract_text=APPLICATION_WIKI_DECISION_CONTRACT_TEXT,
            contract_evidence_ref=(
                f"{APPLICATION_WIKI_DECISION_CONTRACT_ID}"
                f"#{APPLICATION_WIKI_DECISION_CONTRACT_REVISION}"
            ),
            source_id=f"application-wiki-write:{source_digest[:40]}",
            source_revision_id=f"application-wiki-write:{source_digest}",
            source_content_hash=source_facts_hash,
            source_uri=f"application-wiki-write://{source_digest[:40]}",
            evidence_refs=tuple(
                dict.fromkeys(
                    [
                        f"wiki-write:{page_path}",
                        f"principal:{principal_facts['principal_id']}",
                        *source_facts["evidence_refs"],
                    ]
                )
            ),
            task=f"Write application Wiki page {page_path}",
            goal="Persist only the exact ACL-authorized application page mutation.",
            constraints=(
                "The target must remain within the configured Wiki vault.",
                "The rendered content and observed before hash must remain exact.",
            ),
            created_at=operation_created_at,
            scope_prefix="application-wiki-write",
            producer="application-trusted-write-bridge",
            producer_version=APPLICATION_WIKI_DECISION_CONTRACT_REVISION,
            producer_code_hash=APPLICATION_WIKI_DECISION_PRODUCER_HASH,
            evaluator_id="application-wiki-write-evaluator",
            evaluator=evaluator,
        )
    )
    material_action = resolver(expected_request)
    return service.submit_markdown(
        target_path=target,
        content=content,
        source="application_facade_wiki_write",
        actor=principal_facts["agent"],
        source_session_id=str(session_id or ""),
        evidence_refs=list(
            dict.fromkeys(
                [f"wiki_write:{page_path}", *source_facts["evidence_refs"]]
            )
        ),
        proposed_action="application_wiki_write",
        expected_existing_hash=expected_existing_hash,
        metadata={
            "entrypoint": "wiki_write",
            "page_path": page_path,
            "frontmatter_keys": sorted(frontmatter_keys),
        },
        material_action=material_action,
    )


def _validate_wiki_target(target: Path, wiki_resolved: Path, page_path: str) -> dict[str, Any]:
    try:
        target.relative_to(wiki_resolved)
    except ValueError:
        return {"success": False, "message": f"路径超出 Wiki 目录范围: {page_path}"}
    if target == wiki_resolved:
        return {"success": False, "message": f"Wiki 页面路径不能是 Wiki 根目录: {page_path}"}
    try:
        target.parent.relative_to(wiki_resolved)
    except ValueError:
        return {"success": False, "message": f"Wiki 页面父目录超出 Wiki 目录范围: {page_path}"}
    return {}


def _render_frontmatter(frontmatter_data: Mapping[str, Any]) -> str:
    try:
        import yaml

        frontmatter_yaml = yaml.safe_dump(
            dict(frontmatter_data),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        return "\n".join(["---", frontmatter_yaml.rstrip(), "---"])
    except ImportError:
        frontmatter_lines = ["---"]
        for key, value in frontmatter_data.items():
            if isinstance(value, list):
                frontmatter_lines.append(f"{key}: [{', '.join(str(item) for item in value)}]")
            else:
                frontmatter_lines.append(f"{key}: {value}")
        frontmatter_lines.append("---")
        return "\n".join(frontmatter_lines)
