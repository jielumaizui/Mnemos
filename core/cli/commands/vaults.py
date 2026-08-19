"""Vaults command for Mnemos CLI."""

import json
import logging
import subprocess
from pathlib import Path

from core.cli.helpers import _get_config, _print_vault_status
from core.vaults.content_audit import audit_vault_content, format_content_audit
from core.vaults.link_audit import (
    LINK_AUDIT_SCOPE_PREFIXES,
    audit_vault_links,
    repair_broken_wikilinks,
    repair_vault_absolute_wikilinks,
    render_link_audit_report,
    render_link_repair_report,
)
from core.vaults.placement_audit import (
    audit_vault_placement,
    format_placement_audit,
    format_placement_repair,
    repair_identical_duplicate_basenames,
)
from core.vaults.vault_sync import sync_all_projections

logger = logging.getLogger(__name__)


def _vault_git_dirty(vault_dir: Path) -> str:
    """Return git status --short output for a vault, or empty when clean/untracked by git."""
    if not (vault_dir / ".git").exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(vault_dir), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("[vaults] git status failed for %s: %s", vault_dir, exc)
        return ""
    if result.returncode != 0:
        logger.warning("[vaults] git status failed for %s: %s", vault_dir, result.stderr)
        return ""
    return result.stdout.strip()


def _refuse_dirty_apply(vault_dir: Path, allow_dirty: bool) -> bool:
    """Print a guard message and return True when an apply operation must stop."""
    if allow_dirty:
        return False
    dirty = _vault_git_dirty(vault_dir)
    if not dirty:
        return False
    print("Vault is dirty; commit/clean it first or pass --allow-dirty.")
    print(dirty[:2000])
    return True


def _vault_arg_or_default(args) -> Path:
    vault_arg = getattr(args, "vault", None)
    if vault_arg:
        return Path(vault_arg).expanduser()
    return Path(_get_config().vault_dir("mnemos"))


def cmd_vaults(args):
    """Vault 管理入口：手动全量重建认知层 Markdown 投影。"""
    if args.vaults_cmd == "sync":
        config = _get_config()
        vault_dir = config.vault_dir("mnemos")
        raw_dir = config.vault_dir("raw")
        if not getattr(args, "apply", False) or getattr(args, "dry_run", False):
            print("Vault 投影同步 dry-run")
            print("=" * 40)
            print(f"目标 vault: {vault_dir}")
            print(f"Raw vault:  {raw_dir}")
            print("未执行写入；加 --apply 才会重建认知 Vault 投影")
            return 0

        if _refuse_dirty_apply(vault_dir, getattr(args, "allow_dirty", False)):
            return 2

        print("开始重建认知 Vault Markdown 投影...")
        result = sync_all_projections(commit=not args.no_commit)
        print(f"目标 vault: {result['vault_dir']}")
        print(f"KG 投影:    {result['kg']}")
        print(f"Observation: {result['observation']}")
        print(f"Reflection: {result['reflection']}")
        print(f"Persona:    {result['persona']}")
        git = result.get("git", {})
        print(f"Git 快照:   {'✓' if git.get('committed') else '✗'} {git.get('output', '')[:200]}")
        return 0
    elif args.vaults_cmd == "status":
        config = _get_config()
        status, warnings = _print_vault_status(config)
        print(status)
        for w in warnings:
            print(f"⚠ {w}")
        return 0
    elif args.vaults_cmd == "audit-placement":
        config = _get_config()
        vault_dir = config.vault_dir("mnemos")
        report = audit_vault_placement(vault_dir)
        if getattr(args, "json", False):
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_placement_audit(report))
        return 0
    elif args.vaults_cmd == "audit-content":
        config = _get_config()
        vault_dir = config.vault_dir("mnemos")
        report = audit_vault_content(vault_dir)
        if getattr(args, "json", False):
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_content_audit(report))
        return 0
    elif args.vaults_cmd == "audit-links":
        vault_dir = _vault_arg_or_default(args)
        report = audit_vault_links(
            vault_dir,
            sample_limit=getattr(args, "limit", 20),
            scope=getattr(args, "scope", "all"),
        )
        if getattr(args, "json", False):
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(render_link_audit_report(report))
        return 0 if report.ok else 1
    elif args.vaults_cmd == "repair-links":
        vault_dir = _vault_arg_or_default(args)
        apply_changes = getattr(args, "apply", False)
        if apply_changes and _refuse_dirty_apply(
            vault_dir, getattr(args, "allow_dirty", False)
        ):
            return 2
        repair_fn = (
            repair_broken_wikilinks
            if getattr(args, "strip_broken", False)
            else repair_vault_absolute_wikilinks
        )
        report = repair_fn(
            vault_dir,
            sample_limit=getattr(args, "limit", 20),
            scope=getattr(args, "scope", "all"),
            apply=apply_changes,
        )
        if getattr(args, "json", False):
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(render_link_repair_report(report))
        return 0 if report.ok else 1
    elif args.vaults_cmd == "repair-placement":
        config = _get_config()
        vault_dir = config.vault_dir("mnemos")
        if getattr(args, "apply", False) and _refuse_dirty_apply(
            vault_dir, getattr(args, "allow_dirty", False)
        ):
            return 2
        report = repair_identical_duplicate_basenames(
            vault_dir,
            apply=getattr(args, "apply", False),
            limit=getattr(args, "limit", None),
        )
        if getattr(args, "json", False):
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_placement_repair(report))
        return 0
    else:
        scopes = "|".join(LINK_AUDIT_SCOPE_PREFIXES)
        print(
            "用法: mnemos vaults "
            "{sync|status|audit-placement|audit-content|audit-links|repair-placement|repair-links} "
            f"[--scope {scopes}]"
        )
        return 1
