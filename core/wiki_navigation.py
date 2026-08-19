"""Deterministic, trusted MOC navigation projection for the Wiki vault."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.cognitive.decision_trace import MaterialActionRequest
from core.cognitive.state_contract import sha256_json
from core.frontmatter import read_markdown
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    authorize_exact_markdown_action,
    submit_or_write_markdown_with_decision,
)
from core.trust.models import sha256_text
from core.trust.markdown_update import trusted_markdown_delete
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
    trusted_markdown_material_action_binding,
)

NAV_DIR = Path("05-MOCs") / "Mnemos-Navigation"
ROOT_NAV = NAV_DIR / "Vault-导航.md"
ROOT_NAV_GUIDE = NAV_DIR / "Vault-导航-说明.md"
NAV_MARKER = "<!-- mnemos-generated-navigation:v1 -->"
PAGE_NAV_MARKER = "<!-- mnemos-page-navigation:v1 -->"
NAV_CHUNK_SIZE = 150

NAVIGATION_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:wiki-navigation-projection",
    contract_revision_id="mnemos.wiki_navigation_projection.v1",
    contract_text=(
        "MOC navigation may write or delete only the exact mutation frozen in an "
        "immutable NavigationPlan derived from the exact vault page set."
    ),
    source_namespace="wiki-navigation-projection",
    producer="moc-navigation",
    producer_code_hash=sha256_json(
        {
            "module": "core.wiki_navigation",
            "producers": ["_write", "_delete"],
            "version": "mnemos.wiki_navigation_projection.v1",
        }
    ),
    evaluator_id="wiki-navigation-projection-evaluator",
    constraints=(
        "Plan operation, target, target preimage, rendered bytes, and vault root remain exact.",
        "Navigation projection may not modify source knowledge content outside its marker block.",
    ),
    approved_candidate_key="apply_exact_navigation_mutation",
    approved_candidate_summary="Apply the exact frozen navigation-plan mutation.",
    rejected_candidate_key="retain_existing_navigation_projection",
    rejected_candidate_summary="Retain navigation state when the plan or bytes drift.",
    approved_reason_code="navigation_plan_binding_verified",
    rejected_reason_code="navigation_plan_binding_rejected",
    committed_metric="navigation_projection_committed",
    rejected_metric="unbound_navigation_projection_count",
)


@dataclass(frozen=True)
class NavigationMutation:
    """One exact formal Markdown mutation derived before projection starts."""

    operation: str
    target_path: Path
    content: str
    existing_content: str | None
    proposed_action: str


@dataclass(frozen=True)
class NavigationPlan:
    """Immutable desired-state plan for one navigation reconciliation."""

    vault_dir: Path
    mutations: tuple[NavigationMutation, ...]
    generated_group_pages: int
    indexed_pages: int
    page_to_nav: tuple[tuple[str, str], ...]


def _iter_pages(vault_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in vault_dir.rglob("*.md")
        if not any(part.startswith(".") for part in path.relative_to(vault_dir).parts)
    )


def _frontmatter(title: str, summary: str) -> str:
    return (
        "---\n"
        f"标题: {title}\n状态: 活跃\n来源数量: 1\n知识阶段: 已整理\n"
        "证据级别: 单源\n来源:\n- wiki-projection-ledger\n领域: Mnemos知识导航\n"
        f"摘要: {summary}\n---\n"
    )


def _wiki_target(path: Path) -> str:
    return path.with_suffix("").as_posix()


def _preamble(title: str) -> str:
    return (
        f"# {title}\n\n{NAV_MARKER}\n\n"
        "本页由 Mnemos 的 Wiki 质量闭环生成，用于为知识页提供稳定、可点击且可重建的入口。"
        "它只维护导航关系，不改写被索引页面的知识正文；页面移动、删除或新增后，由页面生命周期账本重新生成。"
        "链接采用 Vault 相对路径，避免同名页面、绝对路径和历史目录迁移造成歧义。"
        "如页面内容需要修改，应进入目标页完成；本页不承担知识正文，也不替代来源、证据和人工判断。\n\n"
    )


def _generated_pages(vault_dir: Path) -> set[Path]:
    nav_root = vault_dir / NAV_DIR
    if not nav_root.exists():
        return set()
    return {
        page
        for page in nav_root.glob("*.md")
        if NAV_MARKER in read_markdown(page)
    }


def _publish(path: Path, mutation_type: str, *, page_id: str = "") -> None:
    from core.wiki_projection_publisher import publish_wiki_page_updated

    publish_wiki_page_updated(
        path,
        update_type=mutation_type,
        previous_path=path if mutation_type == "delete" else None,
        page_id=page_id,
        source="moc_navigation",
    )


def _write(
    vault_dir: Path, path: Path, content: str, *, publish_mutations: bool
) -> tuple[int, int, str]:
    existed = path.is_file()
    existing = read_markdown(path) if existed else None
    if existing == content:
        return 0, 0, ""
    evidence_refs = [f"navigation_target:{path.relative_to(vault_dir)}"]
    result = submit_or_write_markdown_with_decision(
        decision_policy=NAVIGATION_MARKDOWN_POLICY,
        decision_facts={
            "schema_version": "mnemos.wiki_navigation_mutation_facts.v1",
            "operation": "update" if existed else "create",
            "target_path": str(path.resolve(strict=False)),
            "vault_dir": str(vault_dir.resolve(strict=False)),
            "publish_mutations": publish_mutations,
        },
        decision_task=f"Rebuild navigation page {path.name}",
        decision_goal="Apply the exact frozen navigation projection for this page.",
        decision_created_at=datetime.now(timezone.utc).isoformat(),
        wiki_base=vault_dir,
        target_path=path,
        content=content,
        source="moc_navigation",
        actor="system",
        evidence_refs=evidence_refs,
        proposed_action="rebuild_navigation_page",
        expected_existing_hash=sha256_text(existing) if existing is not None else None,
        metadata={"projection": "moc_navigation"},
    )
    if result.intercepted:
        return 0, 1, result.proposal_id
    if publish_mutations:
        _publish(path, "update" if existed else "create")
    return 1, 0, ""


def _delete(
    vault_dir: Path, path: Path, *, publish_mutations: bool
) -> tuple[int, int, str]:
    if not path.is_file():
        return 0, 0, ""
    identity: dict[str, Any] = {}
    if publish_mutations:
        from core.wiki_projection_publisher import publish_wiki_page_updated

        identity = publish_wiki_page_updated(
            path, update_type="update", source="moc_navigation"
        )
    existing = read_markdown(path)
    evidence_refs = [f"navigation_target:{path.relative_to(vault_dir)}"]
    material_action = authorize_exact_markdown_action(
        policy=NAVIGATION_MARKDOWN_POLICY,
        wiki_base=vault_dir,
        target_path=path,
        content="",
        proposed_action="delete_stale_navigation_page",
        expected_existing_hash=sha256_text(existing),
        source_facts={
            "schema_version": "mnemos.wiki_navigation_mutation_facts.v1",
            "operation": "delete",
            "target_path": str(path.resolve(strict=False)),
            "vault_dir": str(vault_dir.resolve(strict=False)),
            "publish_mutations": publish_mutations,
        },
        evidence_refs=evidence_refs,
        task=f"Delete stale navigation page {path.name}",
        goal="Remove only the exact stale generated navigation page.",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    result = trusted_markdown_delete(
        vault_dir,
        path,
        "moc_navigation",
        "delete_stale_navigation_page",
        evidence_refs=evidence_refs,
        metadata={"projection": "moc_navigation"},
        material_action=material_action,
    )
    if result.intercepted:
        return 0, 1, result.proposal_id
    if publish_mutations:
        _publish(path, "delete", page_id=str(identity["page_id"]))
    return 1, 0, ""


def plan_navigation(vault_dir: Path) -> NavigationPlan:
    """Derive every exact navigation mutation without executing a write."""

    vault_dir = Path(vault_dir).expanduser().resolve()
    existing_generated = _generated_pages(vault_dir)
    nav_root = vault_dir / NAV_DIR
    pages = []
    for page in _iter_pages(vault_dir):
        try:
            page.relative_to(nav_root)
        except ValueError:
            pages.append(page)

    page_to_nav: dict[str, str] = {}
    group_paths: list[Path] = []
    desired: set[Path] = set()
    mutations: list[NavigationMutation] = []

    def plan_write(target: Path, content: str, proposed_action: str) -> None:
        """Append one changed navigation page to the immutable plan."""

        existing = read_markdown(target) if target.is_file() else None
        if existing != content:
            mutations.append(
                NavigationMutation(
                    operation="write",
                    target_path=target,
                    content=content,
                    existing_content=existing,
                    proposed_action=proposed_action,
                )
            )

    for index in range(0, len(pages), NAV_CHUNK_SIZE):
        chunk = pages[index : index + NAV_CHUNK_SIZE]
        group_no = index // NAV_CHUNK_SIZE + 1
        rel_nav = NAV_DIR / f"Vault-导航-{group_no:03d}.md"
        group_paths.append(rel_nav)
        title = f"Vault 导航 {group_no:03d}"
        lines = [
            _frontmatter(title, f"Mnemos Wiki 第 {group_no:03d} 组稳定导航入口"),
            _preamble(title),
            f"返回总入口：[[{_wiki_target(ROOT_NAV)}]]\n",
            "## 页面\n",
        ]
        for page in chunk:
            rel = page.relative_to(vault_dir)
            lines.append(f"- [[{_wiki_target(rel)}]]")
            page_to_nav[rel.as_posix()] = _wiki_target(rel_nav)
        target = vault_dir / rel_nav
        desired.add(target)
        plan_write(
            target,
            "\n".join(lines) + "\n",
            "rebuild_navigation_page",
        )

    root_title = "Mnemos Vault 导航"
    root_lines = [
        _frontmatter(root_title, "Mnemos Wiki 全量分组导航与生命周期入口"),
        _preamble(root_title),
        f"维护说明：[[{_wiki_target(ROOT_NAV_GUIDE)}]]\n",
        "## 分组入口\n",
        *(f"- [[{_wiki_target(path)}]]" for path in group_paths),
    ]
    root_target = vault_dir / ROOT_NAV
    desired.add(root_target)
    plan_write(
        root_target,
        "\n".join(root_lines) + "\n",
        "rebuild_navigation_page",
    )

    guide_title = "Mnemos Vault 导航说明"
    guide = [
        _frontmatter(guide_title, "解释自动导航的边界、更新方式与恢复依据"),
        _preamble(guide_title),
        f"总入口：[[{_wiki_target(ROOT_NAV)}]]\n\n",
        "## 更新与恢复\n\n",
        "导航页来自当前 Vault 文件清单，采用确定性排序和固定分组大小。页面正文、frontmatter 来源字段与历史内容不会被导航生成器覆盖。",
        "如果文件被移动或删除，Wiki mutation ledger 会记录 move/delete revision；重新执行本投影后，旧导航链接会被替换为当前路径。",
        "任何批量修复都应保留逐文件 SHA-256 备份清单，以便从备份恢复，而不依赖 Git 工作区是否干净。",
    ]
    guide_target = vault_dir / ROOT_NAV_GUIDE
    desired.add(guide_target)
    plan_write(
        guide_target,
        "\n".join(guide) + "\n",
        "rebuild_navigation_page",
    )

    for page in sorted(existing_generated - desired, key=str):
        mutations.append(
            NavigationMutation(
                operation="delete",
                target_path=page,
                content="",
                existing_content=read_markdown(page),
                proposed_action="delete_stale_navigation_page",
            )
        )

    home = vault_dir / "00-Mnemos-Home.md"
    if home.exists():
        text = read_markdown(home)
        block = f"\n\n{PAGE_NAV_MARKER}\n## Vault 导航\n\n- [[{_wiki_target(ROOT_NAV)}]]\n"
        text = text.split(PAGE_NAV_MARKER, 1)[0].rstrip() + block
        plan_write(home, text, "rebuild_navigation_page")
    return NavigationPlan(
        vault_dir=vault_dir,
        mutations=tuple(mutations),
        generated_group_pages=len(group_paths),
        indexed_pages=len(pages),
        page_to_nav=tuple(sorted(page_to_nav.items())),
    )


def navigation_material_action_requests(
    plan: NavigationPlan,
    *,
    state_db_path: Path,
) -> tuple[MaterialActionRequest, ...]:
    """Bind every planned Markdown byte change to its exact target and preimage."""

    requests: list[MaterialActionRequest] = []
    for mutation in plan.mutations:
        expected_hash = (
            sha256_text(mutation.existing_content)
            if mutation.existing_content is not None
            else None
        )
        binding = trusted_markdown_material_action_binding(
            target_path=mutation.target_path,
            content=mutation.content,
            proposed_action=mutation.proposed_action,
            expected_existing_hash=expected_hash,
        )
        requests.append(
            MaterialActionRequest(
                owner=TRUSTED_MARKDOWN_OWNER,
                executor_id=TRUSTED_MARKDOWN_EXECUTOR,
                action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=str(state_db_path),
            )
        )
    return tuple(requests)


def apply_navigation_plan(
    plan: NavigationPlan,
    *,
    publish_mutations: bool = True,
) -> dict[str, Any]:
    """Execute only the mutations frozen in ``plan``."""

    changed = proposed = removed = 0
    proposal_ids: list[str] = []
    for mutation in plan.mutations:
        if mutation.operation == "write":
            wrote, queued, proposal_id = _write(
                plan.vault_dir,
                mutation.target_path,
                mutation.content,
                publish_mutations=publish_mutations,
            )
            changed += wrote
        elif mutation.operation == "delete":
            wrote, queued, proposal_id = _delete(
                plan.vault_dir,
                mutation.target_path,
                publish_mutations=publish_mutations,
            )
            changed += wrote
            removed += wrote
        else:
            raise ValueError(f"unsupported navigation mutation: {mutation.operation}")
        proposed += queued
        if proposal_id:
            proposal_ids.append(proposal_id)
    return {
        "removed_generated_pages": removed,
        "generated_group_pages": plan.generated_group_pages,
        "indexed_pages": plan.indexed_pages,
        "changed_pages": changed,
        "proposed_pages": proposed,
        "proposal_ids": proposal_ids,
        "page_to_nav": dict(plan.page_to_nav),
    }


def rebuild_navigation(
    vault_dir: Path, *, publish_mutations: bool = True
) -> dict[str, Any]:
    """Rebuild navigation via a frozen plan and trusted formal mutations."""

    return apply_navigation_plan(
        plan_navigation(vault_dir),
        publish_mutations=publish_mutations,
    )
