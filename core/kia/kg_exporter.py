# -*- coding: utf-8 -*-
"""
KG Markdown Exporter — L2.4 知识图谱的只读投影

把 EntityManager / KnowledgeGraph 中的实体与关系导出为 Obsidian Markdown：
  L2.4-KG/
    Entities/{entity_slug}.md
    Relations/{source}--{relation_type}--{target}.md
    MOCs/Entity-MOC.md
    MOCs/Relation-Type-MOC.md

注意：
- 本目录是知识图谱的只读投影，直接编辑会在下次导出时被覆盖。
- 如需修改图谱，应通过 KnowledgeGraph API 或蒸馏流程产生关系。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Protocol, Set, Tuple


from core.kia.entity_manager import Entity
from core.kia.relation_schema import Relation, RelationType, RELATION_META
from core.vaults.link_audit import build_vault_target_index, canonical_wiki_target_key
from core.vaults.naming import relation_projection_stem
from core.wiki_derived_projection import (
    DerivedProjectionLifecycle,
    PROJECTION_BINDING_RESERVE_BYTES,
    ProjectionPageSpec,
    canonical_projection_revision,
)

logger = logging.getLogger(__name__)


class EntityProjectionSource(Protocol):
    """Entity reads required by the Markdown projection."""

    def get_all_entities(
        self,
        entity_type: str | None = None,
        min_quality: float = 0.0,
    ) -> List[Entity]:
        """Return the deterministic entity projection denominator."""

        ...

    def get_entity_sources(self, entity_uid: str) -> List[str]:
        """Return canonical source references for one entity."""

        ...


class KnowledgeGraphProjectionSource(Protocol):
    """Public read interface consumed by ``KGExporter``."""

    @property
    def entity_manager(self) -> EntityProjectionSource:
        """Return the read-only entity projection source."""

        ...

    @property
    def projection_ledger_dir(self) -> Path:
        """Return the runtime-consumption ledger directory."""

        ...

    def list_relations_for_projection(self) -> List[Relation]:
        """Return the complete relation projection denominator."""

        ...

    def get_relations(
        self,
        page: str,
        relation_type: RelationType | None = None,
        min_confidence: float = 0.0,
    ) -> List[Relation]:
        """Return outgoing relations for one projected page."""

        ...

    def get_incoming_relations(
        self,
        page: str,
        min_confidence: float = 0.0,
    ) -> List[Relation]:
        """Return incoming relations for one projected page."""

        ...


def _slugify(value: str) -> str:
    """生成文件系统安全的 slug"""
    value = (value or "").strip().lower()
    # 去掉页面路径末尾的 .md，避免导出文件名出现 .md.md
    if value.endswith(".md"):
        value = value[:-3]
    value = re.sub(r'[\\/:*?"<>|]', "-", value)
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-") or "unknown"


def _entity_projection_stem(value: str) -> str:
    """Return the basename used for KG entity projection files.

    Obsidian treats page basenames as a global namespace. KG entity pages are
    rebuildable projections, so their file basenames must not reuse formal
    knowledge page basenames such as ``Python.md``.
    """
    return f"kg-{_slugify(value)}"


def _safe(value: Optional[str]) -> str:
    if value is None:
        return ""
    return value.replace('"', '\\"').replace("\n", " ")


class KGExporter:
    """知识图谱 Markdown 导出器"""

    # 投影数量上限，防止 L2.4-KG 目录无限膨胀
    MAX_EXPORTED_ENTITIES = 500
    MAX_EXPORTED_RELATIONS = 200
    MAX_RELATIONS_PER_ENTITY = 5
    MAX_ENTITY_FILE_BYTES = 50 * 1024  # 50 KB

    # 高质量关系白名单与置信度门槛
    RELATION_CONFIDENCE_THRESHOLD = 0.7
    RELATION_CONFIDENCE_THRESHOLD_TITLE_CONTAINMENT = 0.65
    RELATION_SOURCE_METHOD_WHITELIST = {
        "link_parse",
        "anti_pattern_match",
        "distill",
        "title_containment",
    }

    def __init__(
        self,
        vault_dir: str,
        kg: KnowledgeGraphProjectionSource,
        *,
        lifecycle: DerivedProjectionLifecycle | None = None,
        emit_runtime_consumption: bool = True,
    ):
        self.vault_dir = Path(vault_dir).expanduser().resolve(strict=False)
        self.base_dir = self.vault_dir / "L2.4-KG"
        self.entities_dir = self.base_dir / "Entities"
        self.relations_dir = self.base_dir / "Relations"
        self.mocs_dir = self.base_dir / "MOCs"
        for d in (self.entities_dir, self.relations_dir, self.mocs_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.kg = kg
        self.lifecycle = lifecycle or DerivedProjectionLifecycle(self.vault_dir)
        self.emit_runtime_consumption = bool(emit_runtime_consumption)
        self._pending_pages: List[ProjectionPageSpec] | None = None
        self._projected_entity_file_stems: Set[str] = set()
        self._vault_target_index = build_vault_target_index(self.vault_dir)

    # ───────────────────────────────
    # 实体导出
    # ───────────────────────────────

    def export_entities(self, entities: Optional[Iterable[Entity]] = None) -> Dict[str, Path]:
        """导出实体列表；默认导出 EntityManager 中高质量实体（有数量限制）"""
        if entities is None:
            entities = self.kg.entity_manager.get_all_entities(min_quality=0.0)

        sorted_entities = self._select_export_entities(entities)
        self._projected_entity_file_stems = {
            _entity_projection_stem(entity.uid) for entity in sorted_entities
        }

        written: Dict[str, Path] = {}
        for entity in sorted_entities:
            path = self._write_entity(entity)
            written[entity.uid] = path
        return written

    def _select_export_entities(self, entities: Iterable[Entity]) -> List[Entity]:
        """Select the closed entity subset that may be linked from KG projection files."""
        return sorted(
            (entity for entity in entities if entity is not None),
            key=lambda entity: (
                -float(entity.quality_score),
                -float(entity.confidence),
                str(entity.uid),
            ),
        )[: self.MAX_EXPORTED_ENTITIES]

    def _write_entity(self, entity: Entity) -> Path:
        file_path = self.entities_dir / f"{_entity_projection_stem(entity.uid)}.md"
        normalized_source = self._normalized_source_page(entity.source_page)
        source_refs = list(
            dict.fromkeys(
                normalized
                for source in self.kg.entity_manager.get_entity_sources(entity.uid)
                if (normalized := self._normalized_source_page(source))
            )
        )
        if normalized_source and normalized_source not in source_refs:
            source_refs.insert(0, normalized_source)
        if not source_refs:
            source_refs = [f"knowledge_graph.db#entity:{entity.uid}"]

        frontmatter = {
            "uid": entity.uid,
            "name": entity.name,
            "entity_type": entity.entity_type,
            "source_page": normalized_source,
            "quality_score": round(entity.quality_score, 3),
            "confidence": round(entity.confidence, 3),
            "status": entity.status,
            "temporal_scope": entity.temporal_scope,
            "version_info": entity.version_info,
            "source_count": max(1, entity.source_count, len(source_refs)),
            "sources": source_refs,
            "knowledge_stage": "P2",
            "evidence_level": "single" if entity.source_count <= 1 else "multiple",
            "visit_count": entity.visit_count,
            "tags": sorted(entity.tags) if entity.tags else [],
            "aliases": entity.aliases or [],
        }

        lines = []
        lines.append("---")
        for k, v in frontmatter.items():
            if v is None:
                lines.append(f"{k}:")
            elif isinstance(v, bool):
                lines.append(f"{k}: {str(v).lower()}")
            elif isinstance(v, (list, set, tuple)):
                if not v:
                    lines.append(f"{k}: []")
                else:
                    lines.append(f"{k}:")
                    for item in v:
                        lines.append(f'  - "{_safe(str(item))}"')
            elif isinstance(v, str):
                lines.append(f'{k}: "{_safe(v)}"')
            else:
                lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")

        lines.append(f"# {entity.name}")
        lines.append("")
        lines.append(f"- **类型**: {entity.entity_type}")
        lines.append(f"- **质量**: {entity.quality_score:.3f}")
        lines.append(f"- **置信度**: {entity.confidence:.3f}")
        lines.append(f"- **状态**: {entity.status}")
        lines.append("")

        if entity.source_page:
            link = self._make_wiki_link(entity.source_page)
            lines.append(f"**来源页面**: {link}")
            lines.append("")

        if entity.aliases:
            lines.append("**别名**: " + ", ".join(f"`{a}`" for a in entity.aliases))
            lines.append("")

        # 关联关系预览（出边 + 入边）
        out_rels = self.kg.get_relations(entity.name, min_confidence=0.0)
        in_rels = self.kg.get_incoming_relations(entity.name, min_confidence=0.0)
        if out_rels or in_rels:
            lines.append("## 关联关系")
            lines.append("")
            lines.append("| 方向 | 关系 | 目标 | 强度 | 置信度 |")
            lines.append("|------|------|------|------|--------|")
            for rel in out_rels[:20]:
                lines.append(
                    f"| 出 | {rel.relation_type.value} | {self._make_wiki_link(rel.target)} | "
                    f"{rel.strength:.2f} | {rel.confidence:.2f} |"
                )
            for rel in in_rels[:20]:
                lines.append(
                    f"| 入 | {rel.relation_type.value} | {self._make_wiki_link(rel.source)} | "
                    f"{rel.strength:.2f} | {rel.confidence:.2f} |"
                )
            lines.append("")

        lines.append("## 投影说明")
        lines.append("")
        lines.append(
            "本页是 KnowledgeGraph 中该实体的只读、可重建投影；稳定身份、来源页面与关系端点"
            "以知识图谱数据库为准。即使当前没有关联关系，本页仍保留实体的类型、质量、置信度和"
            "来源引用，供 Obsidian 导航、关系审计与后续增量投影使用。"
        )
        lines.append("")

        lines.append("## 用户备注")
        lines.append("")
        lines.append("```")
        lines.append("# 你可以在这里补充实体的上下文或判断")
        lines.append("```")
        lines.append("")
        lines.append(
            "<!-- 注意：直接编辑此处不会同步回系统。如需修改图谱，请通过 KnowledgeGraph API。 -->"
        )
        lines.append("")

        self._publish_page(
            file_path,
            self._cap_entity_markdown(lines),
            page_role="formal_derived:knowledge_graph_entity",
            source_refs=(f"knowledge_graph.db#entity:{entity.uid}", *source_refs),
        )
        return file_path

    def _cap_entity_markdown(self, lines: List[str]) -> str:
        """Return entity markdown within MAX_ENTITY_FILE_BYTES."""
        content = "\n".join(lines)
        content_budget = max(
            0,
            self.MAX_ENTITY_FILE_BYTES - PROJECTION_BINDING_RESERVE_BYTES,
        )
        if len(content.encode("utf-8")) <= content_budget:
            return content

        notice = "<!-- KG entity projection truncated to fit MAX_ENTITY_FILE_BYTES. -->"
        notice_size = len(notice.encode("utf-8"))
        if notice_size >= content_budget:
            return notice.encode("utf-8")[:content_budget].decode(
                "utf-8",
                errors="ignore",
            )

        capped: List[str] = []
        for line in lines:
            candidate = "\n".join(capped + [line, "", notice])
            if len(candidate.encode("utf-8")) > content_budget:
                break
            capped.append(line)

        while capped and capped[-1] == "":
            capped.pop()

        return "\n".join(capped + ["", notice])

    # ───────────────────────────────
    # 关系导出
    # ───────────────────────────────

    def export_relations(self, relations: Optional[Iterable[Relation]] = None) -> Dict[str, Path]:
        """导出高质量关系子集；默认从数据库筛选（有数量限制）

        过滤规则：
        - source_method 白名单：link_parse / anti_pattern_match / distill / title_containment
        - title_containment 置信度 >= 0.65，其余白名单 >= 0.7
        - 单个实体最多 5 条出边
        - 全局最多 200 条关系
        """
        if relations is None:
            relations = self._fetch_all_relations()

        relation_list = list(relations)
        final_relations = self._select_export_relations(relation_list)
        standalone_generation = self._pending_pages is None
        if standalone_generation:
            self._pending_pages = []
        try:
            written: Dict[str, Path] = {}
            for rel in final_relations:
                path = self._write_relation(rel)
                written[f"{rel.source}--{rel.relation_type.value}--{rel.target}"] = path
            pages = tuple(self._pending_pages or ()) if standalone_generation else ()
        finally:
            if standalone_generation:
                self._pending_pages = None

        if standalone_generation:
            generation = self.lifecycle.publish_generation(
                projection_kind="knowledge_graph",
                scope_root=self.relations_dir,
                pages=pages,
                full=True,
                owned_paths=tuple(page.path for page in pages),
            )
            if generation.status != "committed":
                raise RuntimeError("KG relation projection generation did not commit")
            self._record_runtime_consumption(final_relations)

        if written:
            logger.info(
                "[KGExporter] 导出 %s 条高质量关系（原始 %s 条）",
                len(written),
                len(relation_list),
            )
        return written

    def _select_export_relations(self, relations: Iterable[Relation]) -> List[Relation]:
        """Select the high-quality relation files that may be linked from MOCs."""
        filtered = []
        for rel in relations:
            method = rel.source_method or ""
            if method not in self.RELATION_SOURCE_METHOD_WHITELIST:
                continue
            threshold = (
                self.RELATION_CONFIDENCE_THRESHOLD_TITLE_CONTAINMENT
                if method == "title_containment"
                else self.RELATION_CONFIDENCE_THRESHOLD
            )
            if rel.confidence < threshold:
                continue
            filtered.append(rel)

        filtered.sort(
            key=lambda r: (
                -float(r.confidence),
                -float(r.strength),
                r.source,
                r.relation_type.value,
                r.target,
                r.source_method or "",
                r.context or "",
            )
        )

        entity_out_count: Dict[str, int] = defaultdict(int)
        limited = []
        for rel in filtered:
            source = rel.source
            if entity_out_count[source] >= self.MAX_RELATIONS_PER_ENTITY:
                continue
            entity_out_count[source] += 1
            limited.append(rel)

        return limited[: self.MAX_EXPORTED_RELATIONS]

    def _record_runtime_consumption(self, relations: Iterable[Relation]) -> None:
        """Commit display-consumption receipts only after projection publication."""

        if not self.emit_runtime_consumption:
            return
        from core.ops.runtime_flow_telemetry import (
            record_runtime_consumed,
            runtime_item_id,
        )
        from core.ops.producer_consumer_ledger import ProducerConsumerLedger

        selected_item_ids = {
            runtime_item_id("kg-relation", rel.source, rel.target, rel.relation_type.value)
            for rel in relations
        }
        try:
            pending = ProducerConsumerLedger(
                self.kg.projection_ledger_dir,
                initialize=False,
                read_only=True,
            ).pending_productions(
                "kg_confidence_to_relation_display",
                "core/kia/kg_exporter.py",
            )
        except (FileNotFoundError, sqlite3.Error, ValueError):
            pending = []
        for production in pending:
            record_runtime_consumed(
                "kg_confidence_to_relation_display",
                source="core/kia/kg_exporter.py",
                item_id=production["item_id"],
                production_event_id=production["event_id"],
                metadata={
                    "transition": "relation_projection_decided",
                    "selected": production["item_id"] in selected_item_ids,
                },
                config_or_path=self.kg.projection_ledger_dir,
            )

    def _fetch_all_relations(self) -> List[Relation]:
        """从数据库读取所有关系（含证据）"""
        return self.kg.list_relations_for_projection()

    def _write_relation(self, relation: Relation) -> Path:
        rel_type = relation.relation_type.value
        file_name = f"{self._relation_file_stem(relation)}.md"
        file_path = self.relations_dir / file_name

        meta = RELATION_META.get(relation.relation_type, {})
        description = meta.get("description", rel_type)
        example = meta.get("example", "")

        frontmatter = {
            "source": relation.source,
            "target": relation.target,
            "relation_type": rel_type,
            "strength": round(relation.strength, 3),
            "confidence": round(relation.confidence, 3),
            "source_method": relation.source_method,
            "description": description,
            "status": "active",
            "source_count": max(1, len(relation.evidence or [])),
            "sources": [f"knowledge_graph.db#relation:{self._relation_file_stem(relation)}"],
            "knowledge_stage": "P2",
            "evidence_level": "single" if len(relation.evidence or []) <= 1 else "multiple",
        }

        lines = []
        lines.append("---")
        for k, v in frontmatter.items():
            if v is None:
                lines.append(f"{k}:")
            elif isinstance(v, bool):
                lines.append(f"{k}: {str(v).lower()}")
            elif isinstance(v, (list, set, tuple)):
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f'  - "{_safe(str(item))}"')
            elif isinstance(v, str):
                lines.append(f'{k}: "{_safe(v)}"')
            else:
                lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")

        lines.append(f"# {relation.source} → {rel_type} → {relation.target}")
        lines.append("")
        lines.append(f"> {description}")
        if example:
            lines.append(f"> 示例: {example}")
        lines.append("")

        lines.append(f"- **源**: {self._make_wiki_link(relation.source)}")
        lines.append(f"- **目标**: {self._make_wiki_link(relation.target)}")
        lines.append(f"- **强度**: {relation.strength:.3f}")
        lines.append(f"- **置信度**: {relation.confidence:.3f}")
        lines.append(f"- **方法**: {relation.source_method}")
        lines.append("")

        if relation.evidence:
            lines.append("## 证据")
            lines.append("")
            for i, ev in enumerate(relation.evidence, 1):
                content = (ev.content or "").replace("\n", " ")[:300]
                content = content.replace("[[", r"\[\[").replace("]]", r"\]\]")
                lines.append(f"{i}. **{ev.evidence_type}**: {content}")
            lines.append("")

        lines.append("## 用户备注")
        lines.append("")
        lines.append("```")
        lines.append("# 你可以在这里补充对这条关系的判断")
        lines.append("```")
        lines.append("")
        lines.append(
            "<!-- 注意：直接编辑此处不会同步回系统。如需修改关系，请通过 KnowledgeGraph API。 -->"
        )
        lines.append("")

        self._publish_page(
            file_path,
            "\n".join(lines),
            page_role="formal_derived:knowledge_graph_relation",
            source_refs=(
                f"knowledge_graph.db#relation:{self._relation_file_stem(relation)}",
            ),
        )
        return file_path

    # ───────────────────────────────
    # MOC 导出
    # ───────────────────────────────

    def export_mocs(
        self,
        entities: Optional[Iterable[Entity]] = None,
        relations: Optional[Iterable[Relation]] = None,
    ) -> Tuple[Path, Path]:
        """导出实体与关系类型的 MOC 索引页"""
        if entities is None:
            entities = self.kg.entity_manager.get_all_entities(min_quality=0.0)
        if relations is None:
            relations = self._fetch_all_relations()

        entity_moc = self._write_entity_moc(entities)
        relation_moc = self._write_relation_type_moc(relations)
        return entity_moc, relation_moc

    def _write_entity_moc(self, entities: Iterable[Entity]) -> Path:
        file_path = self.mocs_dir / "Entity-MOC.md"
        entities = list(entities)

        by_type: Dict[str, List[Entity]] = defaultdict(list)
        for e in entities:
            by_type[e.entity_type].append(e)

        frontmatter = {
            "title": "Entity MOC",
            "entity_count": len(entities),
            "status": "active",
            "source_count": 1,
            "sources": ["knowledge_graph.db#entities"],
            "knowledge_stage": "P2",
            "evidence_level": "single",
        }

        lines = []
        lines.append("---")
        for k, v in frontmatter.items():
            if isinstance(v, (list, set, tuple)):
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f'  - "{_safe(str(item))}"')
            elif isinstance(v, str):
                lines.append(f'{k}: "{_safe(v)}"')
            else:
                lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")
        lines.append("# Entity MOC")
        lines.append("")
        lines.append(f"> 共 {len(entities)} 个实体。本页自动生成，是知识图谱的只读投影。")
        lines.append("")

        for entity_type in sorted(by_type.keys()):
            lines.append(f"## {entity_type}")
            lines.append("")
            sorted_entities = sorted(
                by_type[entity_type], key=lambda e: e.quality_score, reverse=True
            )
            for e in sorted_entities:
                entity_path = f"L2.4-KG/Entities/{_entity_projection_stem(e.uid)}"
                lines.append(f"- [[{entity_path}|{e.name}]] `q={e.quality_score:.2f}`")
            lines.append("")

        self._publish_page(
            file_path,
            "\n".join(lines),
            page_role="formal_derived:knowledge_graph_navigation",
            source_refs=("knowledge_graph.db#entities",),
        )
        return file_path

    def _write_relation_type_moc(self, relations: Iterable[Relation]) -> Path:
        file_path = self.mocs_dir / "Relation-Type-MOC.md"
        relations = list(relations)

        by_type: Dict[str, List[Relation]] = defaultdict(list)
        for r in relations:
            by_type[r.relation_type.value].append(r)

        frontmatter = {
            "title": "Relation Type MOC",
            "relation_count": len(relations),
            "status": "active",
            "source_count": 1,
            "sources": ["knowledge_graph.db#relations"],
            "knowledge_stage": "P2",
            "evidence_level": "single",
        }

        lines = []
        lines.append("---")
        for k, v in frontmatter.items():
            if isinstance(v, (list, set, tuple)):
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f'  - "{_safe(str(item))}"')
            elif isinstance(v, str):
                lines.append(f'{k}: "{_safe(v)}"')
            else:
                lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")
        lines.append("# Relation Type MOC")
        lines.append("")
        lines.append(f"> 共 {len(relations)} 条关系。本页自动生成，是知识图谱的只读投影。")
        lines.append("")

        for rel_type in sorted(by_type.keys()):
            rels = by_type[rel_type]
            meta = RELATION_META.get(RelationType(rel_type), {})
            desc = meta.get("description", rel_type)
            lines.append(f"## {rel_type}")
            lines.append(f"> {desc}")
            lines.append("")
            for r in sorted(rels, key=lambda x: x.confidence, reverse=True):
                rel_path = f"L2.4-KG/Relations/{self._relation_file_stem(r)}"
                lines.append(
                    f"- [[{rel_path}|{r.source} → {r.relation_type.value} → {r.target}]] "
                    f"`c={r.confidence:.2f}`"
                )
            lines.append("")

        self._publish_page(
            file_path,
            "\n".join(lines),
            page_role="formal_derived:knowledge_graph_navigation",
            source_refs=("knowledge_graph.db#relations",),
        )
        return file_path

    # ───────────────────────────────
    # 全量导出
    # ───────────────────────────────

    def export_to_vault(self) -> Dict[str, int]:
        """全量导出到 L2.4-KG/，返回统计

        仅清理 lifecycle 已绑定但本代不再生成的页面；目录内未绑定页面
        不推断为本投影所有，历史遗留项交给显式 reconciliation 分类。
        """
        entities = list(self.kg.entity_manager.get_all_entities(min_quality=0.0))
        relations = list(self._fetch_all_relations())
        projected_entities = self._select_export_entities(entities)
        self._projected_entity_file_stems = {
            _entity_projection_stem(entity.uid) for entity in projected_entities
        }
        projected_relations = self._select_export_relations(relations)

        if self._pending_pages is not None:
            raise RuntimeError("KG projection generation is already active")
        self._pending_pages = []
        try:
            entity_files = self.export_entities(projected_entities)
            relation_files = self.export_relations(projected_relations)
            self.export_mocs(projected_entities, projected_relations)
            pages = tuple(self._pending_pages)
        finally:
            self._pending_pages = None
        generation = self.lifecycle.publish_generation(
            projection_kind="knowledge_graph",
            scope_root=self.base_dir,
            pages=pages,
            full=True,
            owned_paths=tuple(page.path for page in pages),
        )
        if generation.status != "committed":
            raise RuntimeError("KG projection generation did not commit")
        self._record_runtime_consumption(projected_relations)

        return {
            "entities": len(entity_files),
            "relations": len(relation_files),
        }

    def _publish_page(
        self,
        path: Path,
        content: str,
        *,
        page_role: str,
        source_refs: tuple[str, ...],
    ) -> None:
        """Collect or publish one typed KG projection page.

        The revision binds only stable render inputs. Database ingestion clocks
        are operational state and cannot change a rebuildable projection.
        """

        page = ProjectionPageSpec(
            path=path,
            content=content,
            page_role=page_role,
            canonical_revision=canonical_projection_revision(
                {
                    "content": content,
                    "page_role": page_role,
                    "source_refs": source_refs,
                }
            ),
            source_refs=source_refs,
        )
        if self._pending_pages is not None:
            self._pending_pages.append(page)
            return
        generation = self.lifecycle.publish_generation(
            projection_kind="knowledge_graph",
            scope_root=self.base_dir,
            pages=[page],
            full=False,
        )
        if generation.status != "committed":
            raise RuntimeError(f"KG projection page was not published: {path}")

    # ───────────────────────────────
    # 工具
    # ───────────────────────────────

    def _make_wiki_link(self, value: str) -> str:
        """把实体名/页面路径转为 Obsidian wiki link"""
        if not value:
            return "`—`"
        if value.endswith(".md"):
            path = Path(value).expanduser()
            if path.is_absolute():
                try:
                    rel = path.resolve(strict=False).relative_to(self.vault_dir)
                except ValueError:
                    return path.name
            else:
                rel = path
            candidate = self.vault_dir / rel
            if candidate.is_file():
                return f"[[{rel.with_suffix('').as_posix()}]]"
            targets = self._vault_target_index.get(canonical_wiki_target_key(value), ())
            if len(targets) == 1:
                return f"[[{Path(targets[0]).with_suffix('').as_posix()}]]"
            return value
        entity_stem = _entity_projection_stem(value)
        if entity_stem in self._projected_entity_file_stems:
            return f"[[L2.4-KG/Entities/{entity_stem}|{value}]]"
        return value

    def _normalized_source_page(self, value: str) -> str:
        if not value:
            return ""
        path = Path(value).expanduser()
        if not path.is_absolute():
            return path.as_posix()
        try:
            return path.resolve(strict=False).relative_to(self.vault_dir).as_posix()
        except ValueError:
            return ""

    @staticmethod
    def _relation_file_stem(relation: Relation) -> str:
        return relation_projection_stem(
            relation.source,
            relation.relation_type.value,
            relation.target,
        )
