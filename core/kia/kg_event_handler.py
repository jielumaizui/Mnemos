# -*- coding: utf-8 -*-
"""
KGEventHandler — 知识图谱事件处理器

订阅 knowledge_distilled 事件，蒸馏完成后实时更新实体和关系。
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from core.cognitive.decision_trace import MaterialActionRequest
from core.config import get_config
from core.kia.relation_schema import Relation
from core.kia.relation_endpoint_quality import is_derived_kg_scan_path
from core.trust.markdown_adapter import read_markdown_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KGEventMaterialPlan:
    """Read-only relation plan derived from one immutable Wiki event."""

    event_kind: str
    page_contents: tuple[tuple[str, str], ...] = ()
    discovered_relations: tuple[Relation, ...] = ()
    distill_knowledge_graph_relations: tuple[Relation, ...] = ()
    distill_relations: tuple[Relation, ...] = ()
    implicit_relations: tuple[Relation, ...] = ()


class KGEventHandler:
    """知识图谱事件处理器

    事件驱动：订阅蒸馏完成事件，自动更新知识图谱。
    """

    def __init__(
        self,
        *,
        db_path: Optional[Path | str] = None,
        wiki_base: Optional[Path | str] = None,
        embedding_index_dir: Optional[Path | str] = None,
        embedding_client: Any | None = None,
        config: Any | None = None,
        projection_lifecycle: Any | None = None,
        emit_projection_runtime_consumption: bool = True,
    ):
        self._db_path = Path(db_path).expanduser() if db_path else None
        self._wiki_base = Path(wiki_base).expanduser() if wiki_base else None
        self._embedding_index_dir = (
            Path(embedding_index_dir).expanduser() if embedding_index_dir else None
        )
        self._embedding_client = embedding_client
        self._runtime_config = config
        self._projection_lifecycle = projection_lifecycle
        self._emit_projection_runtime_consumption = bool(
            emit_projection_runtime_consumption
        )
        self._entity_manager = None
        self._relation_manager = None
        self._deferred_page_update_kg = None
        self._deferred_projection_requested = False

    def _get_entity_manager(self):
        if self._entity_manager is None:
            from .entity_manager import EntityManager

            self._entity_manager = EntityManager(db_path=self._db_path)
        return self._entity_manager

    def _get_relation_manager(self):
        if self._relation_manager is None:
            from .relation_manager import RelationManager

            self._relation_manager = RelationManager(
                str(self._db_path) if self._db_path is not None else None
            )
        return self._relation_manager

    def _planning_relation_manager(self):
        """Return a no-DDL relation reader for pre-authorization planning."""

        if self._relation_manager is not None:
            return self._relation_manager
        from .relation_manager import RelationManager

        return RelationManager(
            str(self._db_path) if self._db_path is not None else None,
            initialize=False,
        )

    def _new_knowledge_graph(
        self,
        *,
        initialize: bool = True,
        read_only: bool = False,
    ):
        if (
            initialize
            and not read_only
            and self._deferred_page_update_kg is not None
        ):
            return self._deferred_page_update_kg
        from .knowledge_graph import KnowledgeGraph

        return KnowledgeGraph(
            db_path=str(self._db_path) if self._db_path is not None else None,
            wiki_base=str(self._wiki_base) if self._wiki_base is not None else None,
            embedding_index_dir=(
                str(self._embedding_index_dir)
                if self._embedding_index_dir is not None
                else None
            ),
            initialize=initialize,
            read_only=read_only,
            embedding_client=self._embedding_client,
            config=self._runtime_config,
        )

    @contextmanager
    def deferred_page_update_replay(self):
        """Batch costly KG side effects while preserving each event entrypoint."""

        if self._deferred_page_update_kg is not None:
            raise RuntimeError("deferred KG page-update replay is already active")
        kg = self._new_knowledge_graph()
        self._deferred_page_update_kg = kg
        self._deferred_projection_requested = False
        succeeded = False
        try:
            with kg.defer_relation_embeddings():
                yield
            succeeded = True
        finally:
            projection_requested = self._deferred_projection_requested
            self._deferred_page_update_kg = None
            self._deferred_projection_requested = False
            try:
                if succeeded and projection_requested:
                    self._project_kg_to_vault(kg)
            finally:
                kg.close()

    def _is_derived_projection_path(self, page_path: Path | str) -> bool:
        if self._wiki_base is None:
            return False
        try:
            page = Path(page_path).expanduser().resolve(strict=False)
            page.relative_to(self._wiki_base.resolve(strict=False))
        except ValueError:
            return False
        return is_derived_kg_scan_path(page, self._wiki_base.resolve(strict=False))

    def plan_on_distilled(self, event: Mapping[str, Any]) -> KGEventMaterialPlan:
        """Derive every guarded relation effect before event execution starts."""

        from .entity_manager import EntityManager

        wiki_pages = tuple(str(value) for value in event.get("wiki_pages", []) if value)
        if not wiki_pages:
            return KGEventMaterialPlan(event_kind="knowledge_distilled")
        kg = self._new_knowledge_graph(initialize=False, read_only=True)
        rm = self._planning_relation_manager()
        existing_pages = kg._candidate_existing_pages()
        page_contents: list[tuple[str, str]] = []
        discovered: list[Relation] = []
        entity_names: list[str] = []
        for raw_path in wiki_pages:
            page = Path(raw_path)
            if not page.exists():
                continue
            try:
                content = read_markdown_text(page)
            except (OSError, IOError):
                continue
            page_contents.append((str(page), content))
            entity_names.extend(EntityManager.extract_entity_names(content))
            discovered.extend(
                kg.discover_relations(
                    page,
                    existing_pages=existing_pages,
                    new_content=content,
                )
            )

        kg_input = event.get("kg_input", {}) or {}
        distill_relations = rm.plan_distill_relations(kg_input)
        explicit_kg_relations = kg.prepare_discovered_relations(
            [copy.deepcopy(relation) for relation in distill_relations],
            min_confidence=0.7,
        )
        prepared_discovered = kg.prepare_discovered_relations(
            discovered,
            min_confidence=0.7,
        )
        entity_names.extend(
            str(value) for value in kg_input.get("entities", []) if value
        )
        implicit_relations = self._plan_implicit_relations(
            rm,
            entity_names,
        )
        kg.close()
        return KGEventMaterialPlan(
            event_kind="knowledge_distilled",
            page_contents=tuple(page_contents),
            discovered_relations=tuple(prepared_discovered),
            distill_knowledge_graph_relations=tuple(explicit_kg_relations),
            distill_relations=tuple(distill_relations),
            implicit_relations=tuple(implicit_relations),
        )

    def plan_on_page_updated(self, event: Mapping[str, Any]) -> KGEventMaterialPlan:
        """Derive guarded relation upserts for one lifecycle-bound page event."""

        page_path = str(event.get("page_path") or "")
        previous_path = str(event.get("previous_path") or "")
        mutation_type = str(
            event.get("mutation_type") or event.get("update_type") or "update"
        )
        if not page_path:
            return KGEventMaterialPlan(event_kind="wiki_page_updated")
        page = Path(page_path)
        page_is_projection = self._is_derived_projection_path(page)
        previous_is_projection = bool(previous_path) and self._is_derived_projection_path(
            previous_path
        )
        if page_is_projection:
            return KGEventMaterialPlan(event_kind="wiki_page_updated")
        if mutation_type == "move" and previous_is_projection:
            mutation_type = "create"
        elif mutation_type in {"move", "delete"}:
            return KGEventMaterialPlan(event_kind="wiki_page_updated")
        if mutation_type not in {"create", "update", "append", "replace", "merge"}:
            return KGEventMaterialPlan(event_kind="wiki_page_updated")
        if not page.exists():
            return KGEventMaterialPlan(event_kind="wiki_page_updated")
        content = read_markdown_text(page)
        kg = self._new_knowledge_graph(initialize=False, read_only=True)
        relations = kg.prepare_discovered_relations(
            kg.discover_relations(page, new_content=content),
            min_confidence=0.4,
        )
        kg.close()
        return KGEventMaterialPlan(
            event_kind="wiki_page_updated",
            page_contents=((str(page), content),),
            discovered_relations=tuple(relations),
        )

    def material_action_requests(
        self,
        plan: KGEventMaterialPlan,
        *,
        state_db_path: Path,
    ) -> tuple[MaterialActionRequest, ...]:
        """Return the exact nested relation bindings frozen by ``plan``."""

        from .knowledge_graph import (
            KG_GRAPH_RELATION_ACTION,
            KG_GRAPH_RELATION_EXECUTOR,
            KG_GRAPH_RELATION_OWNER,
            knowledge_graph_relation_material_action_binding,
        )
        from .relation_manager import (
            KG_RELATION_ACTION,
            KG_RELATION_EXECUTOR,
            KG_RELATION_OWNER,
            RelationManager,
        )

        requests: list[MaterialActionRequest] = []
        state_path = str(state_db_path)
        for relation in (
            *plan.discovered_relations,
            *plan.distill_knowledge_graph_relations,
        ):
            binding = knowledge_graph_relation_material_action_binding(relation)
            requests.append(
                MaterialActionRequest(
                    owner=KG_GRAPH_RELATION_OWNER,
                    executor_id=KG_GRAPH_RELATION_EXECUTOR,
                    action_type=KG_GRAPH_RELATION_ACTION,
                    target_ref=binding["target_ref"],
                    input_hash=binding["input_hash"],
                    expected_state_db=state_path,
                )
            )
        for relations, reason in (
            (plan.distill_relations, "relation_manager.add_from_distill"),
            (
                plan.implicit_relations,
                "relation_manager.apply_implicit_relations",
            ),
        ):
            for relation in relations:
                binding = RelationManager.relation_material_action_binding(
                    relation,
                    reason=reason,
                )
                requests.append(
                    MaterialActionRequest(
                        owner=KG_RELATION_OWNER,
                        executor_id=KG_RELATION_EXECUTOR,
                        action_type=KG_RELATION_ACTION,
                        target_ref=binding["target_ref"],
                        input_hash=binding["input_hash"],
                        expected_state_db=state_path,
                    )
                )
        deduplicated: dict[tuple[str, ...], MaterialActionRequest] = {}
        for request in requests:
            key = (
                request.owner,
                request.executor_id,
                request.action_type,
                request.target_ref,
                request.input_hash,
                request.expected_state_db,
            )
            deduplicated.setdefault(key, request)
        return tuple(deduplicated.values())

    def _plan_implicit_relations(
        self,
        relation_manager: Any,
        entity_names: list[str],
    ) -> list[Relation]:
        cfg = self._runtime_config or get_config()
        if not bool(
            cfg.get("knowledge_graph.implicit_relation_discovery_enabled", True)
        ):
            return []
        max_entities = int(
            cfg.get(
                "knowledge_graph.implicit_relation_max_entities_per_event",
                5,
            )
        )
        if max_entities <= 0:
            return []
        selected = tuple(dict.fromkeys(name for name in entity_names if name))[
            :max_entities
        ]
        if not selected:
            return []
        try:
            suggestions_by_entity = relation_manager.discover_implicit_relations_batch(
                list(selected),
                wiki_dir=self._wiki_base or cfg.wiki_dir,
            )
            relations: list[Relation] = []
            for entity_name in selected:
                relations.extend(
                    relation_manager.plan_implicit_relations(
                        suggestions_by_entity.get(entity_name, []),
                        min_confidence=0.5,
                    )
                )
            return relations
        except (
            OSError,
            UnicodeError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RuntimeError,
        ) as exc:
            logger.warning(
                "[KGEventHandler] 隐式关系规划失败: %s",
                exc,
                exc_info=True,
            )
            return []

    def on_distilled(
        self,
        event: Dict,
        *,
        material_plan: KGEventMaterialPlan | None = None,
    ) -> Dict:
        """蒸馏完成事件处理

        Args:
            event: {
                "type": "knowledge_distilled",
                "session_id": "...",
                "fragments": [...],
                "wiki_pages": ["path1", "path2"],
                "meta": {...}
            }

        Returns:
            处理结果 {entities_created, relations_created, ...}
        """
        result: Dict[str, Any] = {
            "entities_created": 0,
            "entities_updated": 0,
            "relations_discovered": 0,
            "relations_added": 0,
            "relations_implicit": 0,
            "projection_enabled": False,
            "projection_entities": 0,
            "projection_relations": 0,
            "projection_errors": 0,
        }

        wiki_pages = event.get("wiki_pages", [])
        if not wiki_pages:
            return result
        plan = material_plan or self.plan_on_distilled(event)
        if plan.event_kind != "knowledge_distilled":
            raise ValueError("knowledge-distilled material plan kind mismatch")

        em = self._get_entity_manager()
        rm = self._get_relation_manager()

        # 1. Apply only the page bytes and relations frozen by the pre-action plan.
        kg = self._new_knowledge_graph()
        all_entities = []
        for page_path, content in plan.page_contents:
            page = Path(page_path)
            entities = em.ingest_from_wiki(page, content=content)
            all_entities.extend(entities)
        result["relations_added"] += kg.apply_discovered(
            list(plan.discovered_relations),
            min_confidence=0.7,
        )

        # 1.5 从蒸馏输出的 kg_input.entities 显式创建实体
        kg_input = event.get("kg_input", {}) or {}
        for entity_name in kg_input.get("entities", []):
            entity = em.add_entity(
                name=entity_name,
                entity_type="concept",
                wiki_page=wiki_pages[0] if wiki_pages else "",
            )
            if entity:
                all_entities.append(entity)

        result["entities_created"] = sum(1 for e in all_entities if e.source_count == 1)
        result["entities_updated"] = len(all_entities) - result["entities_created"]

        # 2. Commit the exact explicit and implicit relation objects in plan order.
        if plan.distill_relations:
            rm.apply_planned_relations(
                list(plan.distill_relations),
                reason="relation_manager.add_from_distill",
            )
            result["relations_discovered"] = len(plan.distill_relations)
            result["relations_added"] += kg.apply_discovered(
                list(plan.distill_knowledge_graph_relations),
                min_confidence=0.7,
            )
        try:
            result["relations_implicit"] = rm.apply_planned_relations(
                list(plan.implicit_relations),
                reason="relation_manager.apply_implicit_relations",
            )
        except (
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            PermissionError,
            RuntimeError,
        ) as e:
            logger.warning("[KGEventHandler] 隐式关系提交失败: %s", e, exc_info=True)
            result["relations_implicit"] = 0

        # 4. Knowledge-gap closure.  A Wiki projection receipt proves that a
        # page exists, not that it resolves any gap.  An independent coverage
        # recheck must name the exact typed asset IDs it verified.
        try:
            from core.app.blindspot_discovery import BlindspotDiscovery

            cfg = self._runtime_config or get_config()
            projection_receipts = event.get("wiki_projection_receipts", {}) or {}
            coverage_receipts = event.get("knowledge_coverage_receipts", {}) or {}
            resolved = 0
            if coverage_receipts:
                bd = BlindspotDiscovery(
                    wiki_base=str(self._wiki_base or cfg.wiki_dir),
                    db_path=str(Path(cfg.database_dir) / "blindspots.db"),
                )
                for page_path in wiki_pages:
                    page_receipt = projection_receipts.get(page_path, {}) or {}
                    coverage_receipt = coverage_receipts.get(page_path, {}) or {}
                    resolved += bd.resolve_by_wiki_page(
                        page_path,
                        canonical_revision_id=str(
                            page_receipt.get("canonical_revision_id") or ""
                        ),
                        projection_receipt_id=str(
                            page_receipt.get("projection_receipt_id") or ""
                        ),
                        content_hash=str(page_receipt.get("content_hash") or ""),
                        coverage_evidence=tuple(
                            item
                            for item in coverage_receipt.get("resolution_evidence", ())
                            if isinstance(item, Mapping)
                        ),
                    )
            result["blindspots_resolved"] = resolved
            if resolved:
                logger.info("[KGEventHandler] 自动关闭 %s 个盲区", resolved)
        except (
            ImportError,
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RuntimeError,
        ) as e:
            logger.warning("[KGEventHandler] 盲区闭环处理失败: %s", e, exc_info=True)
            result["blindspots_resolved"] = 0
            result["status"] = "retry"
            result["success"] = False
            result["error"] = f"knowledge coverage closure failed: {e}"

        # 5. 受控投影到 L2.4-KG/：KGExporter 内部按置信度、来源、单实体出边和全局上限裁剪。
        result.update(self._project_kg_to_vault(kg))

        logger.info(
            "[KGEventHandler] 蒸馏事件处理完成: entities=%s/%s, relations=%s/%s",
            result["entities_created"],
            result["entities_updated"],
            result["relations_added"],
            result["relations_implicit"],
        )

        return result

    def on_page_updated(
        self,
        event: Dict,
        *,
        material_plan: KGEventMaterialPlan | None = None,
    ) -> Dict:
        """页面更新事件处理

        Args:
            event: {
                "type": "wiki_page_updated",
                "page_path": "...",
                "update_type": "append|replace|merge",
            }
        """
        page_path = event.get("page_path", "")
        previous_path = event.get("previous_path", "")
        mutation_type = str(event.get("mutation_type") or event.get("update_type") or "update")
        if not page_path:
            return {"status": "invalid", "error": "missing page_path"}
        plan = material_plan or self.plan_on_page_updated(event)
        if plan.event_kind != "wiki_page_updated":
            raise ValueError("wiki-page-updated material plan kind mismatch")

        page = Path(page_path)
        page_is_projection = self._is_derived_projection_path(page)
        previous_is_projection = bool(previous_path) and self._is_derived_projection_path(
            previous_path
        )
        if page_is_projection and mutation_type == "move" and not previous_is_projection:
            kg = self._new_knowledge_graph()
            lifecycle: Dict = dict(
                kg.reconcile_page_lifecycle(
                    previous_path=Path(previous_path),
                    page_path=Path(previous_path),
                    mutation_type="delete",
                )
            )
            lifecycle.update(self._project_kg_to_vault(kg))
            lifecycle.update(
                {
                    "status": "ok",
                    "reason": "page moved out of the KG ingestion source set",
                }
            )
            return lifecycle
        if page_is_projection:
            return {
                "status": "skipped",
                "reason": "knowledge graph projection is not an ingestion source",
            }
        if mutation_type == "move" and previous_is_projection:
            mutation_type = "create"
        if mutation_type in {"move", "delete"}:
            kg = self._new_knowledge_graph()
            lifecycle = dict(
                kg.reconcile_page_lifecycle(
                    previous_path=Path(previous_path or page_path),
                    page_path=page,
                    mutation_type=mutation_type,
                    replacement_relations=list(plan.discovered_relations),
                )
            )
            lifecycle.update(self._project_kg_to_vault(kg))
            lifecycle["status"] = "ok"
            return lifecycle
        if not plan.page_contents:
            return {"status": "page_not_found"}

        # A create/update is a replacement of this page's prior graph
        # contribution. Retract it before ingesting the frozen new contents so
        # retries and historical replay converge instead of accumulating stale
        # relations and entity-source attributions.
        kg = self._new_knowledge_graph()
        replacement = {
            "entities_updated": 0,
            "relations_deleted": 0,
        }
        if mutation_type in {"create", "update"}:
            replacement = dict(
                kg.reconcile_page_lifecycle(
                    previous_path=Path(previous_path or page_path),
                    page_path=page,
                    mutation_type=mutation_type,
                )
            )

        # 重新提取实体（更新质量分）
        em = self._get_entity_manager()
        planned_page, content = plan.page_contents[0]
        if Path(planned_page).resolve(strict=False) != page.resolve(strict=False):
            raise ValueError("wiki page material plan target mismatch")
        entities = em.ingest_from_wiki(page, content=content)

        # Apply only the relation objects frozen before the projection scope.
        added = kg.apply_discovered(
            list(plan.discovered_relations),
            min_confidence=0.4,
        )

        projection = self._project_kg_to_vault(kg)

        return {
            "status": "ok",
            "entities_updated": len(entities),
            "entity_sources_retracted": int(replacement.get("entities_updated", 0)),
            "relations_added": added,
            "relations_deleted": int(replacement.get("relations_deleted", 0)),
            **projection,
        }

    def reconcile_pages(self, page_paths, *, replace_existing: bool = False) -> Dict:
        """Consume one current-state page closure and refresh its projection once."""

        em = self._get_entity_manager()
        kg = self._new_knowledge_graph()
        pages_processed = 0
        pages_skipped = 0
        entities_updated = 0
        relations_added = 0
        errors = []
        page_contents = []
        try:
            with kg.defer_relation_embeddings() as embedding_batch:
                for raw_path in page_paths:
                    page = Path(raw_path).expanduser()
                    if self._is_derived_projection_path(page):
                        pages_skipped += 1
                        continue
                    try:
                        content = read_markdown_text(page)
                        if replace_existing:
                            kg.reconcile_page_lifecycle(
                                previous_path=page,
                                page_path=page,
                                mutation_type="update",
                            )
                        entities = em.ingest_from_wiki(page, content=content)
                        entities_updated += len(entities)
                        page_contents.append((page, content))
                    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                        errors.append({"page_path": str(page), "error": str(exc)})

                kg.normalize_entity_primary_sources()
                # Every discovery in this closure must see the same final
                # entity/source state and the same parsed candidate snapshot.
                existing_pages = kg._candidate_existing_pages()
                candidate_cache = kg.prepare_relation_candidates(existing_pages)
                for page, content in page_contents:
                    try:
                        discovered = kg.discover_relations(
                            page,
                            existing_pages=existing_pages,
                            new_content=content,
                            candidate_cache=candidate_cache,
                        )
                        relations_added += kg.apply_discovered(
                            discovered, min_confidence=0.4
                        )
                        pages_processed += 1
                    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                        errors.append({"page_path": str(page), "error": str(exc)})

            projection = self._project_kg_to_vault(kg)
            if int(projection.get("projection_errors", 0)):
                errors.append(
                    {"page_path": "L2.4-KG", "error": "projection refresh failed"}
                )
            return {
                "status": "error" if errors else "ok",
                "pages_processed": pages_processed,
                "pages_skipped": pages_skipped,
                "entities_updated": entities_updated,
                "relations_added": relations_added,
                "replace_existing": replace_existing,
                "embedding_batch": embedding_batch,
                "errors": errors,
                **projection,
            }
        finally:
            kg.close()

    def _project_kg_to_vault(self, kg) -> Dict[str, int | bool]:
        cfg = self._runtime_config or get_config()
        if not bool(cfg.get("knowledge_graph.projection_enabled", True)):
            return {
                "projection_enabled": False,
                "projection_entities": 0,
                "projection_relations": 0,
                "projection_errors": 0,
            }
        if self._deferred_page_update_kg is not None:
            self._deferred_projection_requested = True
            return {
                "projection_enabled": True,
                "projection_deferred": True,
                "projection_entities": 0,
                "projection_relations": 0,
                "projection_errors": 0,
            }
        from .kg_exporter import KGExporter

        exporter_kwargs: dict[str, Any] = {"kg": kg}
        if self._projection_lifecycle is not None:
            exporter_kwargs["lifecycle"] = self._projection_lifecycle
        if not self._emit_projection_runtime_consumption:
            exporter_kwargs["emit_runtime_consumption"] = False
        exporter = KGExporter(
            str(self._wiki_base or cfg.wiki_dir),
            **exporter_kwargs,
        )
        exporter.MAX_EXPORTED_RELATIONS = int(
            cfg.get(
                "knowledge_graph.projection_max_relations",
                exporter.MAX_EXPORTED_RELATIONS,
            )
        )
        exporter.MAX_RELATIONS_PER_ENTITY = int(
            cfg.get(
                "knowledge_graph.projection_max_relations_per_entity",
                exporter.MAX_RELATIONS_PER_ENTITY,
            )
        )
        stats = exporter.export_to_vault()
        return {
            "projection_enabled": True,
            "projection_entities": int(stats.get("entities", 0)),
            "projection_relations": int(stats.get("relations", 0)),
            "projection_errors": 0,
        }

    def on_entity_accessed(self, entity_name: str) -> None:
        """实体被访问事件（用于时间衰减计算 / 访问质量更新）。

        Args:
            entity_name: 实体 UID、名称或别名。
        """
        if not entity_name:
            return
        try:
            em = self._get_entity_manager()
            # 兼容 UID、名称、别名三种输入
            entity = em.get_entity(entity_name) or em.resolve_alias(entity_name)
            if entity:
                # 小幅增加置信度与质量分
                em.update_quality(entity.uid, 0.7, 0.75)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("[KGEventHandler] 实体访问事件处理失败: %s", entity_name, exc_info=True)
