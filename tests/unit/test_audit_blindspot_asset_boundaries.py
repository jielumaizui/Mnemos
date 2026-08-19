from __future__ import annotations

import json
import sqlite3

from core.app.blindspot_asset_schema import initialize_blindspot_asset_schema
from core.cognitive.user_model_asset_store import (
    INTERACTION_PREFERENCE_SPEC,
    USER_COGNITIVE_BLINDSPOT_SPEC,
    initialize_asset_store,
)
from core.knowledge_form import CANONICAL_KNOWLEDGE_FORMS
from scripts.audit_blindspot_asset_boundaries import (
    _knowledge_form_vocabulary_contract,
    build_report,
)


def _distilled_page(*, form: str | None) -> str:
    form_line = f"知识形态: {form}\n" if form is not None else ""
    return (
        "---\n"
        "名称: COG-016 fixture\n"
        "领域: 测试\n"
        "摘要: 用于验证知识形态生产覆盖合同。\n"
        "蒸馏时间: '2026-07-23 00:00:00'\n"
        f"{form_line}"
        "---\n"
        "# Fixture\n"
    )


def _initialize_three_stores(tmp_path) -> None:
    initialize_blindspot_asset_schema(tmp_path / "blindspots.db")
    initialize_asset_store(tmp_path / "user_cognitive_blindspots.db", USER_COGNITIVE_BLINDSPOT_SPEC)
    initialize_asset_store(tmp_path / "interaction_preferences.db", INTERACTION_PREFERENCE_SPEC)


def test_structural_fixture_is_valid_but_non_certifying_without_runtime_effect_evidence(tmp_path):
    db_path = tmp_path / "blindspots.db"
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    _initialize_three_stores(tmp_path)
    (wiki_dir / "fixture.md").write_text(_distilled_page(form="决策记录"), encoding="utf-8")

    report = build_report(db_path=db_path, wiki_dir=wiki_dir)

    assert report["ok"] is False
    assert report["failures"] == ["runtime_consumer_effect_unobserved"]
    assert report["asset_contract"]["identity_collision_count"] == 0
    assert report["knowledge_form_coverage"]["coverage"] == 1.0
    assert report["asset_contract"]["ok"] is True
    assert report["asset_contract"]["registered_schema_initialization_owners"] == [
        "scripts/reconcile_user_model_asset_stores.py"
    ]
    assert report["asset_contract"]["runtime_writer_implicit_initialization"] is False
    assert report["asset_contract"]["knowledge_form_vocabulary_owner_count"] == 1
    assert report["asset_contract"]["producer_migration_consumer_normalization_drift"] == 0


def test_audit_blocks_distilled_page_without_form(tmp_path):
    db_path = tmp_path / "blindspots.db"
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    _initialize_three_stores(tmp_path)
    (wiki_dir / "missing.md").write_text(_distilled_page(form=None), encoding="utf-8")

    report = build_report(db_path=db_path, wiki_dir=wiki_dir)

    assert report["ok"] is False
    assert "production_knowledge_form_coverage" in report["failures"]
    assert report["knowledge_form_coverage"]["missing_count"] == 1


def test_audit_reports_zero_knowledge_form_denominator_as_unobserved(tmp_path):
    db_path = tmp_path / "blindspots.db"
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    _initialize_three_stores(tmp_path)

    report = build_report(db_path=db_path, wiki_dir=wiki_dir)

    coverage = report["knowledge_form_coverage"]
    assert coverage["eligible_page_count"] == 0
    assert coverage["coverage"] is None
    assert coverage["observation_status"] == "UNOBSERVED"
    assert report["ok"] is False
    assert "production_knowledge_form_coverage" in report["failures"]


def test_structural_audit_does_not_claim_source_strings_prove_consumer_effect(tmp_path):
    db_path = tmp_path / "blindspots.db"
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    _initialize_three_stores(tmp_path)
    (wiki_dir / "fixture.md").write_text(_distilled_page(form="决策记录"), encoding="utf-8")

    report = build_report(db_path=db_path, wiki_dir=wiki_dir)

    contract = report["asset_contract"]
    assert contract["source_scan_is_consumer_effect_evidence"] is False
    assert contract["consumer_effect_evidence_status"] == "UNOBSERVED"
    assert "canonical_consumer_checks" not in contract
    assert report["audit_scope"] == "structural_boundary_non_certifying"
    assert report["ok"] is False
    assert "runtime_consumer_effect_unobserved" in report["failures"]


def test_audit_blocks_legacy_topic_primary_key_store(tmp_path):
    db_path = tmp_path / "blindspots.db"
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE blindspots (topic TEXT PRIMARY KEY)")

    report = build_report(db_path=db_path, wiki_dir=wiki_dir)

    assert report["ok"] is False
    assert "runtime_knowledge_gap_schema" in report["failures"]
    assert report["runtime_schema"]["classification"] == "legacy_topic_table"


def test_audit_blocks_noncanonical_schema_owner(tmp_path):
    db_path = tmp_path / "blindspots.db"
    wiki_dir = tmp_path / "wiki"
    fake_root = tmp_path / "repo"
    wiki_dir.mkdir()
    (fake_root / "core").mkdir(parents=True)
    initialize_blindspot_asset_schema(db_path)
    (fake_root / "core" / "extra.py").write_text(
        "SQL = 'CREATE TABLE knowledge_coverage_gap_revisions (id TEXT)'\n",
        encoding="utf-8",
    )

    report = build_report(db_path=db_path, wiki_dir=wiki_dir, root=fake_root)

    assert report["ok"] is False
    assert report["schema_ddl_owners"] == ["core/extra.py"]
    assert "knowledge_gap_schema_owner" in report["failures"]


def test_audit_blocks_a_second_asset_store_initialization_owner(tmp_path):
    db_path = tmp_path / "blindspots.db"
    wiki_dir = tmp_path / "wiki"
    fake_root = tmp_path / "repo"
    wiki_dir.mkdir()
    (fake_root / "scripts").mkdir(parents=True)
    (fake_root / "scripts" / "reconcile_user_model_asset_stores.py").write_text(
        "from core.cognitive.user_model_asset_store import initialize_asset_store\n"
        "initialize_asset_store(path, spec)\n",
        encoding="utf-8",
    )
    (fake_root / "scripts" / "rogue_initialize.py").write_text(
        "from core.cognitive.user_model_asset_store import initialize_asset_store as init\n"
        "init(path, spec)\n",
        encoding="utf-8",
    )
    initialize_blindspot_asset_schema(db_path)

    report = build_report(db_path=db_path, wiki_dir=wiki_dir, root=fake_root)

    assert report["asset_contract"]["runtime_writer_implicit_initialization"] is True
    assert (
        "scripts/rogue_initialize.py"
        in report["asset_contract"]["registered_schema_initialization_owners"]
    )
    assert "runtime_writer_implicit_initialization" in report["failures"]


def test_knowledge_form_vocabulary_audit_detects_second_owner_and_schema_drift(tmp_path):
    root = tmp_path / "repo"
    (root / "core/hephaestus").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "prompts/distill/_output_schemas").mkdir(parents=True)
    (root / "prompts/distill/extract").mkdir()
    (root / "core/knowledge_form.py").write_text(
        "FORM_ALIASES = {}\n",
        encoding="utf-8",
    )
    (root / "scripts/plan_wiki_knowledge_form_reconciliation.py").write_text(
        "from core.knowledge_form import display_knowledge_form\n",
        encoding="utf-8",
    )
    (root / "core/hephaestus/distillation_wiki_page.py").write_text(
        "normalize_knowledge_form(fragment.form)\n" "knowledge_form_entity_type(form)\n",
        encoding="utf-8",
    )
    (root / "scripts/audit_blindspot_asset_boundaries.py").write_text(
        "normalize_knowledge_form(value)\n",
        encoding="utf-8",
    )
    schema = {
        "properties": {
            "fragments": {
                "items": {"properties": {"form": {"enum": list(CANONICAL_KNOWLEDGE_FORMS)}}}
            }
        }
    }
    schema_path = root / "prompts/distill/_output_schemas/extract.json"
    schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    prompt_rows = "\n".join(
        f"| {form} | definition | evidence |" for form in CANONICAL_KNOWLEDGE_FORMS
    )
    (root / "prompts/distill/extract/base.md").write_text(
        f"### 知识形态（六类）\n\n{prompt_rows}\n\n### 页面格式\n",
        encoding="utf-8",
    )

    baseline = _knowledge_form_vocabulary_contract(root)

    assert baseline["ok"] is True
    assert baseline["knowledge_form_vocabulary_owner_count"] == 1
    assert baseline["producer_migration_consumer_normalization_drift"] == 0

    (root / "scripts/rogue_aliases.py").write_text(
        "FORM_ALIASES = {'insight': '洞察关联'}\n",
        encoding="utf-8",
    )
    second_owner = _knowledge_form_vocabulary_contract(root)

    assert second_owner["ok"] is False
    assert second_owner["knowledge_form_vocabulary_owner_count"] == 2

    schema["properties"]["fragments"]["items"]["properties"]["form"]["enum"] = ["问题-解决"]
    schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    drifted = _knowledge_form_vocabulary_contract(root)

    assert drifted["ok"] is False
    assert drifted["producer_migration_consumer_normalization_drift"] == 1
