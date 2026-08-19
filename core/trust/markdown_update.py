"""Small convenience wrappers around trusted formal Markdown writes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from core.cognitive.decision_trace import MaterialActionAuthorization
from core.trust.formal_markdown import submit_or_write_markdown
from core.trust.models import sha256_text
from core.trust.vault_mutation_service import (
    TrustedVaultMutationResult,
    TrustedVaultMutationService,
    commit_trusted_markdown_delete,
    commit_trusted_markdown_move,
)


def trusted_markdown_update(
    wiki_base: Path,
    target_path: Path,
    content: str,
    source: str,
    proposed_action: str,
    *,
    actor: str = "system",
    evidence_refs: Iterable[str] = (),
    existing_content: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    material_action: MaterialActionAuthorization | None = None,
) -> TrustedVaultMutationResult:
    return submit_or_write_markdown(
        wiki_base=wiki_base,
        target_path=target_path,
        content=content,
        source=source,
        actor=actor,
        evidence_refs=evidence_refs,
        proposed_action=proposed_action,
        expected_existing_hash=sha256_text(existing_content) if existing_content is not None else None,
        metadata=metadata,
        material_action=material_action,
    )


def trusted_markdown_delete(
    wiki_base: Path,
    target_path: Path,
    source: str,
    proposed_action: str,
    *,
    actor: str = "system",
    evidence_refs: Iterable[str] = (),
    metadata: Mapping[str, Any] | None = None,
    material_action: MaterialActionAuthorization | None = None,
) -> TrustedVaultMutationResult:
    """Authorize a formal Markdown deletion before removing the file."""

    target = Path(target_path).expanduser()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    result = TrustedVaultMutationService(wiki_base=wiki_base).submit_markdown(
        target_path=target,
        content="",
        source=source,
        actor=actor,
        evidence_refs=evidence_refs,
        proposed_action=proposed_action,
        expected_existing_hash=sha256_text(existing),
        metadata=metadata,
        material_action=material_action,
    )
    commit_trusted_markdown_delete(
        result,
        target_path=target,
        material_action=material_action,
    )
    return result


def trusted_markdown_move(
    wiki_base: Path,
    source_path: Path,
    target_path: Path,
    content: str,
    existing_source_content: str,
    source: str,
    proposed_action: str,
    *,
    actor: str = "system",
    evidence_refs: Iterable[str] = (),
    metadata: Mapping[str, Any] | None = None,
    material_action: MaterialActionAuthorization | None = None,
) -> TrustedVaultMutationResult:
    """Authorize a formal page move before creating the destination/removing the source."""

    source_file = Path(source_path).expanduser()
    target_file = Path(target_path).expanduser()
    result = TrustedVaultMutationService(wiki_base=wiki_base).submit_markdown(
        target_path=target_file,
        content=content,
        source=source,
        actor=actor,
        evidence_refs=evidence_refs,
        proposed_action=proposed_action,
        metadata={
            **dict(metadata or {}),
            "source_path": str(source_file),
            "source_content_hash": sha256_text(existing_source_content),
            "operation": "move_markdown",
        },
        material_action=material_action,
    )
    commit_trusted_markdown_move(
        result,
        source_path=source_file,
        target_path=target_file,
        content=content,
        material_action=material_action,
    )
    return result
