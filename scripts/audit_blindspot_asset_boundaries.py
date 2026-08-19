#!/usr/bin/env python3
"""Audit the COG-016 knowledge-gap, cognitive-blindspot, and preference boundary."""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.app.blindspot_asset_schema import (
    SCHEMA_VERSION as KNOWLEDGE_GAP_SCHEMA_VERSION,
    read_blindspot_schema_status,
)
from core.app.blindspot_discovery import BlindspotDiscovery
from core.cognitive.user_model_assets import (
    AssetScope,
    InteractionPreference,
    KnowledgeCoverageGap,
    KnowledgeCoverageResolutionEvidence,
    UserCognitiveBlindspot,
)
from core.cognitive.user_model_asset_store import (
    INTERACTION_PREFERENCE_SPEC,
    USER_COGNITIVE_BLINDSPOT_SPEC,
    read_asset_store_state,
)
from core.config import get_config
from core.frontmatter import fm_get
from core.kia.hygieia import KnowledgeImmuneSystem
from core.knowledge_form import (
    CANONICAL_KNOWLEDGE_FORMS,
    display_knowledge_form,
    normalize_knowledge_form,
)

AUDIT_SCHEMA_VERSION = "mnemos.cog016_asset_boundary_audit.v3"
REQUIRED_FORM_COVERAGE = 1.0


def _frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        raise ValueError("unterminated frontmatter")
    payload = yaml.safe_load(text[4:end]) or {}
    if not isinstance(payload, dict):
        raise ValueError("frontmatter must be a mapping")
    return payload


def _knowledge_form_coverage(wiki_dir: Path) -> dict[str, Any]:
    immune = KnowledgeImmuneSystem(wiki_base=str(wiki_dir))
    eligible: list[str] = []
    covered: list[str] = []
    missing: list[str] = []
    invalid: list[dict[str, str]] = []
    parse_errors: list[dict[str, str]] = []
    for page in sorted(immune._list_pages()):
        relative = str(page.relative_to(wiki_dir))
        try:
            frontmatter = _frontmatter(page)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            parse_errors.append({"page": relative, "error": str(exc)})
            continue
        if fm_get(frontmatter, "distilled_at", "") in (None, ""):
            continue
        eligible.append(relative)
        raw_form = fm_get(frontmatter, "form", "")
        values = raw_form if isinstance(raw_form, list) else [raw_form]
        normalized = {normalize_knowledge_form(value) for value in values if str(value).strip()}
        normalized.discard("")
        if normalized:
            covered.append(relative)
        elif raw_form in (None, "", []):
            missing.append(relative)
        else:
            invalid.append({"page": relative, "form": str(raw_form)})
    denominator = len(eligible)
    numerator = len(covered)
    observation_status = "OBSERVED" if denominator else "UNOBSERVED"
    ratio = numerator / denominator if denominator else None
    return {
        "contract": "all_distilled_pages_have_valid_knowledge_form",
        "required_coverage": REQUIRED_FORM_COVERAGE,
        "eligible_page_count": denominator,
        "covered_page_count": numerator,
        "coverage": ratio,
        "observation_status": observation_status,
        "missing_count": len(missing),
        "invalid_count": len(invalid),
        "parse_error_count": len(parse_errors),
        "missing_examples": missing[:20],
        "invalid_examples": invalid[:20],
        "parse_error_examples": parse_errors[:20],
        "ok": bool(
            ratio is not None
            and ratio >= REQUIRED_FORM_COVERAGE
            and not missing
            and not invalid
            and not parse_errors
        ),
    }


def _ddl_owners(root: Path) -> list[str]:
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?:\{REVISION_TABLE\}|knowledge_coverage_gap_revisions)(?=\s|\()",
        re.IGNORECASE,
    )
    owners: list[str] = []
    for directory in ("core", "integrations", "daemon", "scripts"):
        base = root / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if pattern.search(path.read_text(encoding="utf-8")):
                owners.append(str(path.relative_to(root)))
    return owners


def _typed_asset_ddl_owners(root: Path, table: str) -> list[str]:
    pattern = re.compile(
        rf"(?:revision_table\s*=\s*[\"']{re.escape(table)}[\"']|"
        rf"CREATE\s+TABLE\s+{re.escape(table)})(?=\s|\)|,)",
        re.IGNORECASE,
    )
    owners: list[str] = []
    for directory in ("core", "integrations", "daemon", "scripts"):
        base = root / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if pattern.search(path.read_text(encoding="utf-8")):
                owners.append(str(path.relative_to(root)))
    return owners


def _initialization_call_owners(root: Path) -> list[str]:
    owners: list[str] = []
    for directory in ("core", "integrations", "daemon", "scripts"):
        base = root / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError) as exc:
                owners.append(f"parse-error:{path.relative_to(root)}:{type(exc).__name__}")
                continue
            aliases = {
                alias.asname or alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
                if alias.name == "initialize_asset_store"
            }
            if any(
                isinstance(node, ast.Call)
                and (
                    isinstance(node.func, ast.Name)
                    and node.func.id in aliases | {"_install_attached_store"}
                    or isinstance(node.func, ast.Attribute)
                    and node.func.attr == "initialize_asset_store"
                )
                for node in ast.walk(tree)
            ):
                owners.append(str(path.relative_to(root)))
    return owners


def _named_assignment_owners(root: Path, name: str) -> list[str]:
    owners: list[str] = []
    for directory in ("core", "integrations", "daemon", "scripts"):
        base = root / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError) as exc:
                owners.append(f"parse-error:{path.relative_to(root)}:{type(exc).__name__}")
                continue
            owns_name = any(
                (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == name
                        for target in node.targets
                    )
                )
                or (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == name
                )
                for node in ast.walk(tree)
            )
            if owns_name:
                owners.append(str(path.relative_to(root)))
    return owners


def _knowledge_form_vocabulary_contract(root: Path) -> dict[str, Any]:
    owner_paths = _named_assignment_owners(root, "FORM_ALIASES")
    expected_forms = list(CANONICAL_KNOWLEDGE_FORMS)

    schema_path = root / "prompts/distill/_output_schemas/extract.json"
    schema_forms: list[str] = []
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema_forms = list(
            schema["properties"]["fragments"]["items"]["properties"]["form"]["enum"]
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        schema_forms = []

    prompt_path = root / "prompts/distill/extract/base.md"
    prompt_forms: list[str] = []
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
        form_section = prompt_text.split("### 知识形态（六类）", 1)[1].split("### ", 1)[0]
        for line in form_section.splitlines():
            match = re.match(r"^\|\s*([^|]+?)\s*\|", line)
            if not match:
                continue
            value = match.group(1).strip()
            if display_knowledge_form(value):
                prompt_forms.append(value)
    except (OSError, IndexError):
        prompt_forms = []

    plan_path = root / "scripts/plan_wiki_knowledge_form_reconciliation.py"
    renderer_path = root / "core/hephaestus/distillation_wiki_page.py"
    audit_path = root / "scripts/audit_blindspot_asset_boundaries.py"
    plan_source = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else ""
    renderer_source = renderer_path.read_text(encoding="utf-8") if renderer_path.is_file() else ""
    audit_source = audit_path.read_text(encoding="utf-8") if audit_path.is_file() else ""
    normalization_corpus = (
        (" 洞察 ", "insight", "洞察关联"),
        (" INSIGHT ", "insight", "洞察关联"),
        (" ＩＮＳＩＧＨＴ ", "insight", "洞察关联"),
        (" Decision-Log ", "decision", "决策记录"),
        (" 问题-解决 ", "problem-solution", "问题-解决"),
    )
    checks = {
        "single_alias_owner": owner_paths == ["core/knowledge_form.py"],
        "schema_uses_canonical_display_forms": schema_forms == expected_forms,
        "prompt_uses_canonical_display_forms": prompt_forms == expected_forms,
        "migration_imports_canonical_display": (
            "from core.knowledge_form import display_knowledge_form" in plan_source
            and "FORM_ALIASES =" not in plan_source
        ),
        "renderer_imports_canonical_normalizer": (
            "normalize_knowledge_form(fragment.form)" in renderer_source
            and "knowledge_form_entity_type(form)" in renderer_source
        ),
        "audit_imports_canonical_normalizer": ("normalize_knowledge_form(value)" in audit_source),
        "unicode_case_alias_corpus": all(
            normalize_knowledge_form(raw) == normalized and display_knowledge_form(raw) == display
            for raw, normalized, display in normalization_corpus
        ),
    }
    drift_count = sum(not passed for name, passed in checks.items() if name != "single_alias_owner")
    return {
        "owner_paths": owner_paths,
        "knowledge_form_vocabulary_owner_count": len(owner_paths),
        "canonical_display_forms": expected_forms,
        "schema_forms": schema_forms,
        "prompt_forms": prompt_forms,
        "checks": checks,
        "producer_migration_consumer_normalization_drift": drift_count,
        "ok": all(checks.values()),
    }


def _asset_contract(root: Path) -> dict[str, Any]:
    scope = AssetScope(
        scope_type="project",
        scope_id="mnemos",
        purpose="cog016-audit",
    )
    knowledge_gap = KnowledgeCoverageGap.create(
        topic="projection receipts",
        dimension="missing_form",
        description="A decision form is absent.",
        evidence_refs=("audit:knowledge-coverage",),
        scope=scope,
        confidence=0.7,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    cognitive_blindspot = UserCognitiveBlindspot.create(
        blindspot_type="framing",
        description="The options share a premise.",
        evidence_refs=("audit:decision-snapshot",),
        user_goal_ref="goal:audit",
        impact="May exclude a material alternative.",
        scope=scope,
        confidence=0.8,
        expires_at="2099-01-01T00:00:00+00:00",
        invalidation_condition="A later snapshot has independent premises.",
    )
    preference = InteractionPreference.create(
        dimension="interaction_depth",
        value="implementation_ready",
        evidence_refs=("audit:explicit-preference",),
        scope=scope,
        confidence=0.8,
        expires_at="2099-01-01T00:00:00+00:00",
        invalidation_condition="The user requests a shorter response.",
    )
    resolution_evidence = KnowledgeCoverageResolutionEvidence.from_mapping(
        {
            "receipt_id": "audit-coverage-recheck",
            "asset_id": knowledge_gap.asset_id,
            "gap_revision_id": knowledge_gap.revision_id,
            "scope_key": scope.key,
            "verifier_id": "cog016-independent-auditor",
            "verification_method": "authorized-context-requery",
            "content_hash": "sha256:" + "0" * 64,
            "verified_at": "2099-01-01T00:00:00+00:00",
            "outcome": "covered",
        }
    )
    assets = (knowledge_gap, cognitive_blindspot, preference)
    identities = {asset.asset_id for asset in assets}
    consumers = {asset.asset_type: sorted(asset.consumers) for asset in assets}
    required_fields = {
        "knowledge_coverage_gap": {
            "asset_id",
            "revision_id",
            "evidence_refs",
            "scope",
            "confidence",
            "expires_at",
            "supersedes_revision_id",
            "resolution_condition",
            "consumers",
        },
        "user_cognitive_blindspot": {
            "asset_id",
            "revision_id",
            "evidence",
            "scope_id",
            "confidence",
            "expires_at",
            "supersedes_revision_id",
            "invalidation_condition",
            "consumers",
            "user_goal_ref",
            "impact",
        },
        "interaction_preference": {
            "asset_id",
            "revision_id",
            "evidence_refs",
            "scope",
            "confidence",
            "expires_at",
            "supersedes_revision_id",
            "invalidation_condition",
            "consumers",
        },
    }
    classes = {
        "knowledge_coverage_gap": KnowledgeCoverageGap,
        "user_cognitive_blindspot": UserCognitiveBlindspot,
        "interaction_preference": InteractionPreference,
    }
    missing_fields = {
        name: sorted(required_fields[name] - {field.name for field in fields(cls)})
        for name, cls in classes.items()
    }
    detector_source = inspect.getsource(BlindspotDiscovery._detect_blindspots)
    mixed_profile_dependency = "BlindSpotProfileManager" in detector_source
    canonical_detector_dependency = bool(
        "KnowledgeImmuneSystem" in detector_source
        and "detect_knowledge_gaps" in detector_source
        and "QueryCoverageObservation" in detector_source
    )
    state_machine_checks = {
        "knowledge_coverage_gap": knowledge_gap.status == "detected"
        and knowledge_gap.resolution_condition == "verified_knowledge_coverage_recheck",
        "user_cognitive_blindspot": (
            USER_COGNITIVE_BLINDSPOT_SPEC.initial_status == "suspected"
            and USER_COGNITIVE_BLINDSPOT_SPEC.transitions["suspected"]
            == ("confirmed", "dismissed", "expired")
        ),
        "interaction_preference": (
            INTERACTION_PREFERENCE_SPEC.initial_status == "active"
            and INTERACTION_PREFERENCE_SPEC.transitions["active"] == ("invalidated", "expired")
        ),
    }
    confusion_matrix = {
        asset.asset_type: {
            "asset_id": asset.asset_id,
            "identity_prefix": asset.asset_id.split("_", 1)[0] + "_",
            "consumers": sorted(asset.consumers),
        }
        for asset in assets
    }
    initialization_owners = _initialization_call_owners(root)
    form_vocabulary_contract = _knowledge_form_vocabulary_contract(root)
    migration_path = root / "scripts/reconcile_user_model_asset_stores.py"
    migration_source = (
        migration_path.read_text(encoding="utf-8") if migration_path.is_file() else ""
    )
    migration_contract_metrics = {
        "partial_user_model_store_generation": int(
            "ATTACH DATABASE ? AS interaction_store" not in migration_source
            or "before_generation_commit" not in migration_source
        ),
        "asset_migration_without_plan_hash": int(
            "if not expected_plan_hash:" not in migration_source
            or "expected plan hash does not match locked asset state" not in migration_source
        ),
        "backup_overwrite": int(
            "generation_dir.mkdir(parents=False, exist_ok=False)" not in migration_source
        ),
        "second_apply_changed_rows": int("second_apply_changed_rows" not in migration_source),
        "restore_drill_failure": int(
            "_restore_drill(" not in migration_source
            or "_recover_incomplete_generations(" not in migration_source
        ),
    }
    form_migration_path = root / "scripts/reconcile_wiki_knowledge_forms.py"
    form_migration_source = (
        form_migration_path.read_text(encoding="utf-8") if form_migration_path.is_file() else ""
    )
    form_migration_contract_metrics = {
        "apply_without_exact_plan_hash": int(
            "if not expected_plan_hash:" not in form_migration_source
            or "--apply requires --expected-plan-hash" not in form_migration_source
        ),
        "wiki_write_before_offline_lock": int(
            "with offline_migration_lock(database_dir" not in form_migration_source
        ),
        "partial_generation_visible": int(
            'backup_dir / "staged"' not in form_migration_source
            or "runtime_writers_are_inactive" not in form_migration_source
        ),
        "wiki_projection_generation_mismatch": int(
            "_recover_wiki_projection_databases_unlocked" not in form_migration_source
            or "projection_committed" not in form_migration_source
        ),
    }
    return {
        "asset_types": [asset.asset_type for asset in assets],
        "asset_ids": [asset.asset_id for asset in assets],
        "identity_collision_count": len(assets) - len(identities),
        "missing_contract_fields": missing_fields,
        "consumers": consumers,
        "mixed_profile_dependency_count": int(mixed_profile_dependency),
        "canonical_detector_dependency": canonical_detector_dependency,
        "source_scan_is_consumer_effect_evidence": False,
        "consumer_effect_evidence_status": "UNOBSERVED",
        "registered_schema_initialization_owners": initialization_owners,
        "runtime_writer_implicit_initialization": bool(
            set(initialization_owners) - {"scripts/reconcile_user_model_asset_stores.py"}
        ),
        "knowledge_form_vocabulary_owner_count": (
            form_vocabulary_contract["knowledge_form_vocabulary_owner_count"]
        ),
        "producer_migration_consumer_normalization_drift": (
            form_vocabulary_contract["producer_migration_consumer_normalization_drift"]
        ),
        "knowledge_form_vocabulary_contract": form_vocabulary_contract,
        "migration_contract_metrics": migration_contract_metrics,
        "knowledge_form_migration_contract_metrics": (form_migration_contract_metrics),
        "state_machine_checks": state_machine_checks,
        "same_theme_confusion_matrix": confusion_matrix,
        "resolution_evidence_ref": resolution_evidence.evidence_ref,
        "ok": bool(
            len(identities) == len(assets)
            and not any(missing_fields.values())
            and not mixed_profile_dependency
            and canonical_detector_dependency
            and all(state_machine_checks.values())
            and initialization_owners == ["scripts/reconcile_user_model_asset_stores.py"]
            and form_vocabulary_contract["ok"]
            and not any(migration_contract_metrics.values())
            and not any(form_migration_contract_metrics.values())
            and resolution_evidence.asset_id == knowledge_gap.asset_id
            and resolution_evidence.gap_revision_id == knowledge_gap.revision_id
        ),
    }


def build_report(
    *,
    db_path: Path,
    user_cognitive_blindspot_db_path: Path | None = None,
    interaction_preference_db_path: Path | None = None,
    wiki_dir: Path,
    root: Path | None = None,
) -> dict[str, Any]:
    root = root or Path(__file__).resolve().parents[1]
    user_cognitive_blindspot_db_path = user_cognitive_blindspot_db_path or (
        db_path.parent / "user_cognitive_blindspots.db"
    )
    interaction_preference_db_path = interaction_preference_db_path or (
        db_path.parent / "interaction_preferences.db"
    )
    schema_state = read_blindspot_schema_status(db_path).as_dict()
    cognitive_blindspot_schema_state = read_asset_store_state(
        user_cognitive_blindspot_db_path, USER_COGNITIVE_BLINDSPOT_SPEC
    ).as_dict()
    interaction_preference_schema_state = read_asset_store_state(
        interaction_preference_db_path, INTERACTION_PREFERENCE_SPEC
    ).as_dict()
    asset_contract = _asset_contract(root)
    form_coverage = _knowledge_form_coverage(wiki_dir)
    ddl_owners = _ddl_owners(root)
    expected_owner = "core/app/blindspot_asset_schema.py"
    cognitive_blindspot_ddl_owners = _typed_asset_ddl_owners(
        root, USER_COGNITIVE_BLINDSPOT_SPEC.revision_table
    )
    interaction_preference_ddl_owners = _typed_asset_ddl_owners(
        root, INTERACTION_PREFERENCE_SPEC.revision_table
    )
    expected_typed_owner = "core/cognitive/user_model_asset_store.py"
    failures: list[str] = []
    if not asset_contract["ok"]:
        failures.append("typed_asset_contract")
    if ddl_owners != [expected_owner]:
        failures.append("knowledge_gap_schema_owner")
    if not schema_state["ok"]:
        failures.append("runtime_knowledge_gap_schema")
    if cognitive_blindspot_ddl_owners != [expected_typed_owner]:
        failures.append("user_cognitive_blindspot_schema_owner")
    if interaction_preference_ddl_owners != [expected_typed_owner]:
        failures.append("interaction_preference_schema_owner")
    if not cognitive_blindspot_schema_state["ok"]:
        failures.append("runtime_user_cognitive_blindspot_schema")
    if not interaction_preference_schema_state["ok"]:
        failures.append("runtime_interaction_preference_schema")
    if not form_coverage["ok"]:
        failures.append("production_knowledge_form_coverage")
    if asset_contract["runtime_writer_implicit_initialization"]:
        failures.append("runtime_writer_implicit_initialization")
    if asset_contract["consumer_effect_evidence_status"] != "OBSERVED":
        failures.append("runtime_consumer_effect_unobserved")
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_scope": "structural_boundary_non_certifying",
        "certifying": False,
        "knowledge_gap_schema_version": KNOWLEDGE_GAP_SCHEMA_VERSION,
        "ok": not failures,
        "db_path": str(db_path),
        "wiki_dir": str(wiki_dir),
        "asset_contract": asset_contract,
        "schema_ddl_owners": ddl_owners,
        "user_cognitive_blindspot_schema_ddl_owners": cognitive_blindspot_ddl_owners,
        "interaction_preference_schema_ddl_owners": interaction_preference_ddl_owners,
        "runtime_schema": schema_state,
        "runtime_user_cognitive_blindspot_schema": cognitive_blindspot_schema_state,
        "runtime_interaction_preference_schema": interaction_preference_schema_state,
        "knowledge_form_coverage": form_coverage,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--user-cognitive-blindspot-db-path", type=Path)
    parser.add_argument("--interaction-preference-db-path", type=Path)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = get_config()
    try:
        report = build_report(
            db_path=args.db_path or Path(config.database_dir) / "blindspots.db",
            user_cognitive_blindspot_db_path=(
                args.user_cognitive_blindspot_db_path
                or Path(config.database_dir) / "user_cognitive_blindspots.db"
            ),
            interaction_preference_db_path=(
                args.interaction_preference_db_path
                or Path(config.database_dir) / "interaction_preferences.db"
            ),
            wiki_dir=args.wiki_dir or Path(config.wiki_dir),
        )
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
        report = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "ok": False,
            "failures": ["audit_runtime_error"],
            "error": str(exc),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
