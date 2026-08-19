"""
Tests for core.kia.kg_exporter

Covers: empty export, entity export, relation export, MOC generation,
        full vault export via KnowledgeGraph.export_to_vault.
"""

from unittest.mock import MagicMock, patch

import pytest

from core.kia.kg_exporter import KGExporter, _slugify
from core.kia.entity_manager import Entity
from core.kia.relation_schema import Relation, RelationEvidence, RelationType
from core.vaults.naming import relation_projection_stem
from tests.knowledge_graph_decision_fixtures import authorized_knowledge_graph


@pytest.fixture
def kg(tmp_path):
    db_file = tmp_path / "kg.db"
    with patch("core.kia.entity_manager._get_db_path") as mock_db:
        mock_db.return_value = db_file
        graph = authorized_knowledge_graph(
            db_path=str(db_file),
            wiki_base=str(tmp_path / "wiki"),
        )
        yield graph


def test_export_empty_creates_mocs(kg, tmp_path):
    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    stats = exporter.export_to_vault()

    assert stats == {"entities": 0, "relations": 0}

    base = tmp_path / "vault" / "L2.4-KG"
    assert (base / "MOCs" / "Entity-MOC.md").exists()
    assert (base / "MOCs" / "Relation-Type-MOC.md").exists()

    content = (base / "MOCs" / "Entity-MOC.md").read_text(encoding="utf-8")
    assert "共 0 个实体" in content


def test_export_to_vault_preserves_unowned_page_in_projection_root(kg, tmp_path):
    independent = tmp_path / "vault" / "L2.4-KG" / "manual-notes.md"
    independent.parent.mkdir(parents=True)
    independent.write_text("# Manual notes\n", encoding="utf-8")

    KGExporter(str(tmp_path / "vault"), kg=kg).export_to_vault()

    assert independent.read_text(encoding="utf-8") == "# Manual notes\n"


def test_exporter_reads_relations_through_projection_source_interface(tmp_path):
    class EmptyEntitySource:
        def get_all_entities(self, min_quality=0.0):
            return []

        def get_entity_sources(self, entity_uid):
            return []

    class ProjectionSource:
        entity_manager = EmptyEntitySource()
        projection_ledger_dir = tmp_path

        def __init__(self):
            self.relation_reads = 0

        def list_relations_for_projection(self):
            self.relation_reads += 1
            return []

        def get_relations(self, page, relation_type=None, min_confidence=0.0):
            return []

        def get_incoming_relations(self, page, min_confidence=0.0):
            return []

    source = ProjectionSource()
    exporter = KGExporter(str(tmp_path / "vault"), kg=source)

    assert exporter.export_relations() == {}
    assert source.relation_reads == 1


def test_entity_selection_has_stable_total_order_for_equal_scores(kg, tmp_path):
    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    entities = [
        Entity(uid="zeta", name="Zeta", quality_score=0.8, confidence=0.7),
        Entity(uid="alpha", name="Alpha", quality_score=0.8, confidence=0.7),
    ]

    selected = exporter._select_export_entities(entities)

    assert [entity.uid for entity in selected] == ["alpha", "zeta"]


def test_export_entities_writes_files(kg, tmp_path):
    source = tmp_path / "vault" / "03-Tech" / "python.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Python\n", encoding="utf-8")
    kg.entity_manager._upsert_entity(
        "Python", entity_type="technology", wiki_page="03-Tech/python.md"
    )
    kg.entity_manager._upsert_entity(
        "Python", entity_type="technology", wiki_page="04-Concepts/python.md"
    )
    kg.entity_manager._upsert_entity("Docker", entity_type="technology")

    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    exporter.export_entities()

    entities_dir = tmp_path / "vault" / "L2.4-KG" / "Entities"
    assert (entities_dir / "kg-python.md").exists()
    assert (entities_dir / "kg-docker.md").exists()

    content = (entities_dir / "kg-python.md").read_text(encoding="utf-8")
    assert "Python" in content
    assert "technology" in content
    assert "[[03-Tech/python]]" in content
    assert '"03-Tech/python.md"' in content
    assert '"04-Concepts/python.md"' in content
    sparse_content = (entities_dir / "kg-docker.md").read_text(encoding="utf-8")
    assert len(sparse_content.split("---", 2)[2].strip()) >= 200


def test_entity_projection_omits_rebuild_clock_fields(kg, tmp_path):
    entity = kg.entity_manager._upsert_entity("Python", entity_type="technology")
    entity.first_seen = "2026-01-01T00:00:00"
    entity.last_updated = "2026-07-22T10:00:00"
    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    exporter._pending_pages = []

    exporter._write_entity(entity)

    content = exporter._pending_pages[0].content
    assert "first_seen:" not in content
    assert "last_updated:" not in content


def test_exporter_does_not_emit_broken_or_absolute_wikilinks(kg, tmp_path):
    outside = tmp_path / "outside.md"
    kg.entity_manager._upsert_entity(
        "Outside", entity_type="technology", wiki_page=str(outside)
    )
    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    exporter.export_to_vault()
    content = (tmp_path / "vault" / "L2.4-KG" / "Entities" / "kg-outside.md").read_text(
        encoding="utf-8"
    )
    assert f"[[{outside.with_suffix('')}]]" not in content
    assert "knowledge_stage" in content
    assert "evidence_level" in content
    moc = (tmp_path / "vault" / "L2.4-KG" / "MOCs" / "Entity-MOC.md").read_text(
        encoding="utf-8"
    )
    assert "generated_at" not in moc


def test_entity_projection_filenames_do_not_claim_unbound_legacy_basenames(kg, tmp_path):
    vault = tmp_path / "vault"
    (vault / "03-Tech").mkdir(parents=True)
    (vault / "03-Tech" / "python.md").write_text("# Python\n", encoding="utf-8")
    old_entities = vault / "L2.4-KG" / "Entities"
    old_entities.mkdir(parents=True)
    (old_entities / "python.md").write_text("# stale KG projection\n", encoding="utf-8")
    kg.entity_manager._upsert_entity(
        "Python", entity_type="technology", wiki_page="03-Tech/python.md"
    )

    exporter = KGExporter(str(vault), kg=kg)
    exporter.export_to_vault()

    base = vault / "L2.4-KG"
    assert (base / "Entities" / "python.md").read_text(encoding="utf-8") == (
        "# stale KG projection\n"
    )
    assert (base / "Entities" / "kg-python.md").exists()

    entity_moc = (base / "MOCs" / "Entity-MOC.md").read_text(encoding="utf-8")
    assert "[[L2.4-KG/Entities/kg-python|Python]]" in entity_moc


def test_export_relations_writes_files(kg, tmp_path):
    """高质量白名单来源关系应写入受控 Relations 投影。"""
    relation = Relation(
        source="Python",
        target="Docker",
        relation_type=RelationType.RELATED_TO,
        strength=0.8,
        confidence=0.9,
        source_method="distill",
        evidence=[RelationEvidence(evidence_type="quote", content="两者常一起使用")],
    )
    kg.add_relation(relation)

    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    result = exporter.export_relations()

    rel_dir = tmp_path / "vault" / "L2.4-KG" / "Relations"
    assert len(result) == 2
    rel_file = rel_dir / f"{relation_projection_stem('Python', 'related_to', 'Docker')}.md"
    assert rel_file.exists()
    content = rel_file.read_text(encoding="utf-8")
    assert "Python → related_to → Docker" in content
    assert "两者常一起使用" in content


def test_export_relations_filters_non_whitelisted_methods(kg, tmp_path):
    """非白名单来源关系不写入文件，避免 Relations 目录无限膨胀。"""
    kg.add_relation(
        Relation(
            source="Python",
            target="Docker",
            relation_type=RelationType.RELATED_TO,
            strength=0.8,
            confidence=0.9,
            source_method="manual",
        )
    )

    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    result = exporter.export_relations()

    rel_dir = tmp_path / "vault" / "L2.4-KG" / "Relations"
    assert result == {}
    assert list(rel_dir.iterdir()) == []


def test_relation_projection_selection_is_stable_across_input_order(kg, tmp_path):
    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    exporter.MAX_EXPORTED_RELATIONS = 1
    exporter.MAX_RELATIONS_PER_ENTITY = 1
    relations = [
        Relation(
            source="Alpha",
            target=target,
            relation_type=RelationType.RELATED_TO,
            strength=0.8,
            confidence=0.9,
            source_method="distill",
        )
        for target in ("Zulu", "Beta")
    ]

    forward = exporter._select_export_relations(relations)
    reverse = exporter._select_export_relations(reversed(relations))

    assert [relation.target for relation in forward] == ["Beta"]
    assert [relation.target for relation in reverse] == ["Beta"]


def test_relation_projection_revision_ignores_unrendered_row_timestamps(kg, tmp_path):
    revisions = []
    contents = []
    for name, created_at, updated_at in (
        ("full", "2026-07-22T06:00:00", "2026-07-22T06:01:00"),
        ("incremental", "2026-07-22T03:00:00", "2026-07-22T04:00:00"),
    ):
        exporter = KGExporter(str(tmp_path / name), kg=kg)
        exporter._pending_pages = []
        exporter._write_relation(
            Relation(
                source="Alpha",
                target="Beta",
                relation_type=RelationType.RELATED_TO,
                strength=0.8,
                confidence=0.9,
                source_method="distill",
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        page = exporter._pending_pages[0]
        revisions.append(page.canonical_revision)
        contents.append(page.content)

    assert revisions[0] == revisions[1]
    assert contents[0] == contents[1]


def test_relation_consumption_receipt_waits_for_projection_commit(kg, tmp_path):
    relation = Relation(
        source="Python",
        target="Docker",
        relation_type=RelationType.RELATED_TO,
        strength=0.8,
        confidence=0.9,
        source_method="distill",
    )
    lifecycle = MagicMock()
    lifecycle.publish_generation.side_effect = RuntimeError("event receipt failed")
    exporter = KGExporter(
        str(tmp_path / "vault"),
        kg=kg,
        lifecycle=lifecycle,
    )

    with patch.object(exporter, "_record_runtime_consumption") as record_consumption:
        with pytest.raises(RuntimeError, match="event receipt failed"):
            exporter.export_relations([relation])

    record_consumption.assert_not_called()


def test_export_to_vault_full(kg, tmp_path):
    kg.entity_manager._upsert_entity("Python", entity_type="technology")
    kg.add_relation(
        Relation(
            source="Python",
            target="Docker",
            relation_type=RelationType.RELATED_TO,
            strength=0.8,
            confidence=0.9,
            source_method="distill",
        )
    )

    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    stats = exporter.export_to_vault()

    assert stats == {"entities": 1, "relations": 2}

    base = tmp_path / "vault" / "L2.4-KG"
    assert (base / "Entities" / "kg-python.md").exists()
    assert (
        base / "Relations" / f"{relation_projection_stem('Python', 'related_to', 'Docker')}.md"
    ).exists()
    assert (base / "MOCs" / "Entity-MOC.md").exists()
    assert (base / "MOCs" / "Relation-Type-MOC.md").exists()

    entity_moc = (base / "MOCs" / "Entity-MOC.md").read_text(encoding="utf-8")
    assert "Python" in entity_moc
    assert "[[L2.4-KG/Entities/kg-python|Python]]" in entity_moc

    relation_moc = (base / "MOCs" / "Relation-Type-MOC.md").read_text(encoding="utf-8")
    assert "related_to" in relation_moc


def test_export_to_vault_mocs_only_link_projected_subset(kg, tmp_path):
    alpha = kg.entity_manager._upsert_entity("Alpha", entity_type="concept")
    alpha.quality_score = 1.0
    kg.entity_manager._save_entity(alpha)
    beta = kg.entity_manager._upsert_entity("Beta", entity_type="concept")
    beta.quality_score = 0.1
    kg.entity_manager._save_entity(beta)
    kg.add_relation(
        Relation(
            source="Alpha",
            target="Beta",
            relation_type=RelationType.RELATED_TO,
            strength=0.8,
            confidence=0.9,
            source_method="manual",
        )
    )

    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    exporter.MAX_EXPORTED_ENTITIES = 1
    stats = exporter.export_to_vault()

    assert stats == {"entities": 1, "relations": 0}

    base = tmp_path / "vault" / "L2.4-KG"
    entity_moc = (base / "MOCs" / "Entity-MOC.md").read_text(encoding="utf-8")
    relation_moc = (base / "MOCs" / "Relation-Type-MOC.md").read_text(encoding="utf-8")

    assert "[[L2.4-KG/Entities/kg-alpha|Alpha]]" in entity_moc
    assert "L2.4-KG/Entities/kg-beta" not in entity_moc
    assert "Alpha → related_to → Beta" not in relation_moc


def test_entity_file_includes_relation_preview(kg, tmp_path):
    kg.entity_manager._upsert_entity("Python", entity_type="technology")
    kg.entity_manager._upsert_entity("Docker", entity_type="technology")
    kg.add_relation(
        Relation(
            source="Python",
            target="Docker",
            relation_type=RelationType.USES,
            strength=0.85,
            confidence=0.88,
            source_method="manual",
        )
    )

    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    exporter.export_to_vault()

    content = (tmp_path / "vault" / "L2.4-KG" / "Entities" / "kg-python.md").read_text(
        encoding="utf-8"
    )
    assert "关联关系" in content
    assert "uses" in content
    assert "Docker" in content


def test_entity_file_caps_large_relation_preview_to_max_file_bytes(kg, tmp_path):
    entity = kg.entity_manager._upsert_entity("Python", entity_type="technology")
    for idx in range(24):
        kg.add_relation(
            Relation(
                source="Python",
                target=f"Long Target {idx} {'x' * 80}",
                relation_type=RelationType.RELATED_TO,
                strength=0.8,
                confidence=0.9,
                source_method="manual",
            )
        )

    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    exporter.MAX_ENTITY_FILE_BYTES = 1200
    exporter.export_entities([entity])

    entity_file = tmp_path / "vault" / "L2.4-KG" / "Entities" / "kg-python.md"
    content = entity_file.read_text(encoding="utf-8")

    assert len(entity_file.read_bytes()) <= exporter.MAX_ENTITY_FILE_BYTES
    assert "KG entity projection truncated" in content
    assert content.splitlines().count("---") >= 2


def test_knowledge_graph_export_to_vault_integration(kg, tmp_path):
    vault = tmp_path / "vault"
    kg.entity_manager._upsert_entity("Rust", entity_type="technology")
    kg.add_relation(
        Relation(
            source="Rust",
            target="Cargo",
            relation_type=RelationType.DEPENDS_ON,
            strength=0.9,
            confidence=0.85,
            source_method="distill",
        )
    )

    stats = kg.export_to_vault(str(vault))

    assert stats == {"entities": 1, "relations": 1}
    assert (vault / "L2.4-KG" / "Entities" / "kg-rust.md").exists()
    assert (
        vault
        / "L2.4-KG"
        / "Relations"
        / f"{relation_projection_stem('Rust', 'depends_on', 'Cargo')}.md"
    ).exists()


# ============================================================
# 紧急修复补充测试：.md.md 命名 bug 与旧文件清理
# ============================================================


def test_slugify_strips_md_suffix():
    """_slugify 应去掉末尾 .md，避免导出文件名出现 .md.md"""
    assert _slugify("03-Tech/python.md") == "03-tech-python"
    assert _slugify("00-Inbox/some-page.MD") == "00-inbox-some-page"
    assert _slugify("plain-name") == "plain-name"
    assert _slugify("") == "unknown"
    assert _slugify(None) == "unknown"


def test_export_relation_with_md_paths_avoids_double_md_extension(kg, tmp_path):
    """source/target 以 .md 结尾时，_slugify 不应产生 .md.md。"""
    kg.add_relation(
        Relation(
            source="00-Inbox/source-page.md",
            target="L2.4-KG/Entities/target-page.md",
            relation_type=RelationType.RELATED_TO,
            strength=0.7,
            confidence=0.8,
            source_method="distill",
        )
    )

    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    result = exporter.export_relations()

    rel_dir = tmp_path / "vault" / "L2.4-KG" / "Relations"
    assert len(result) == 2
    rel_stem = relation_projection_stem(
        "00-Inbox/source-page.md",
        "related_to",
        "L2.4-KG/Entities/target-page.md",
    )
    assert (rel_dir / f"{rel_stem}.md").exists()


def test_export_relations_preserves_unbound_markdown(kg, tmp_path):
    """Lifecycle cleanup must not infer ownership from a directory alone."""
    rel_dir = tmp_path / "vault" / "L2.4-KG" / "Relations"
    stale_file = rel_dir / "stale--related_to--relation.md"
    stale_file.parent.mkdir(parents=True, exist_ok=True)
    stale_file.write_text("stale content", encoding="utf-8")

    exporter = KGExporter(str(tmp_path / "vault"), kg=kg)
    exporter.export_relations()

    assert stale_file.read_text(encoding="utf-8") == "stale content"
