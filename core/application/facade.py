# -*- coding: utf-8 -*-
"""Stable application facade used by integration adapters."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping

if TYPE_CHECKING:
    from core.application.cognitive_state import CognitiveStateApplicationService
    from core.evidence.source_authority import SourceAuthorityCatalog

from core.access_policy import (
    AccessNarrowing,
    PrincipalEnvelope,
    authorize_item,
    filter_authorized_items,
    item_access_fields,
)
from core.application.contracts import MnemosServiceFacade  # noqa: F401
from core.application.intelligence import IntelligenceApplicationService
from core.application.kia import KiaApplicationService
from core.application.memory import MemoryApplicationService
from core.application.observation import ObservationApplicationService
from core.application.persona import PersonaApplicationService
from core.application.reflection import ReflectionApplicationService
from core.application.storage import StorageApplicationService
from core.application.facade_capture import (
    APPLICATION_OPERATION_ERRORS,
    FacadeCaptureMixin,
)


class DefaultMnemosServiceFacade(FacadeCaptureMixin):
    """Default facade backed by existing Mnemos services."""

    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger(__name__)
        self._intelligence = IntelligenceApplicationService(self._logger)
        self._kia = KiaApplicationService(self._logger)
        self._memory = MemoryApplicationService(self.wiki_write, self.wiki_search)
        self._observation = ObservationApplicationService()
        self._persona = PersonaApplicationService(self._logger)
        self._reflection = ReflectionApplicationService(self._logger)
        self._storage = StorageApplicationService(self.storage_backend, self._logger)

    def health_check(self) -> Dict:
        from core.ops.health_check import build_health_report_quiet

        return build_health_report_quiet()

    def agent_runtime_probe(
        self,
        source_agent: str,
        health_check_ids_hash: str,
        sample: Dict[str, Any],
    ) -> Dict:
        from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore

        return AgentRuntimeReceiptStore().record_probe(
            source_agent,
            health_check_ids_hash=health_check_ids_hash,
            sample=sample,
        )

    def agent_health_observed(
        self,
        source_agent: str,
        health_check_ids_hash: str,
    ) -> Dict:
        from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore

        return AgentRuntimeReceiptStore().record_health_check(
            source_agent,
            health_check_ids_hash,
        )

    def storage_backend(self) -> Any:
        from core.sync_framework.storage_backend import create_storage_backend

        return create_storage_backend()

    @staticmethod
    def _cognitive_state_application() -> "CognitiveStateApplicationService":
        from core.application.cognitive_state import CognitiveStateApplicationService
        from core.config import get_config

        return CognitiveStateApplicationService(get_config())

    def build_cognitive_state(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = "cognitive_state_read",
    ) -> Dict:
        try:
            return self._cognitive_state_application().build_cognitive_state(
                context,
                principal=principal,
                narrowing=narrowing,
                purpose=purpose,
            )
        except ValueError as exc:
            return self._cognitive_failure("invalid_request", exc)

    def revise_belief(
        self,
        request: Mapping[str, Any],
        *,
        principal: PrincipalEnvelope | None,
    ) -> Dict:
        from core.cognitive.state_schema import CognitiveStateSchemaError
        from core.cognitive.state_store import CognitiveStateConflict

        try:
            return self._cognitive_state_application().revise_belief(
                request,
                principal=principal,
            )
        except PermissionError as exc:
            return self._cognitive_failure("access_denied", exc)
        except RuntimeError as exc:
            return self._cognitive_failure("state_conflict", exc)
        except (
            FileNotFoundError,
            ValueError,
            sqlite3.Error,
            CognitiveStateSchemaError,
            CognitiveStateConflict,
        ) as exc:
            return self._cognitive_failure_for_exception(exc)

    def explain_belief(
        self,
        belief_id: str,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ) -> Dict:
        try:
            return self._cognitive_state_application().explain_belief(
                belief_id,
                principal=principal,
                narrowing=narrowing,
            )
        except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
            return self._cognitive_failure_for_exception(exc)

    def record_decision(
        self,
        trace: Mapping[str, Any],
        *,
        principal: PrincipalEnvelope | None,
        source_authority_catalog: "SourceAuthorityCatalog",
    ) -> Dict:
        from core.cognitive.state_schema import CognitiveStateSchemaError
        from core.cognitive.state_store import CognitiveStateConflict

        try:
            return self._cognitive_state_application().record_decision(
                trace,
                principal=principal,
                source_authority_catalog=source_authority_catalog,
            )
        except (
            FileNotFoundError,
            ValueError,
            sqlite3.Error,
            CognitiveStateSchemaError,
            CognitiveStateConflict,
        ) as exc:
            return self._cognitive_failure_for_exception(exc)

    def apply_outcome(
        self,
        feedback: Mapping[str, Any],
        *,
        principal: PrincipalEnvelope | None,
        source_authority_catalog: "SourceAuthorityCatalog",
    ) -> Dict:
        from core.cognitive.state_schema import CognitiveStateSchemaError
        from core.cognitive.state_store import CognitiveStateConflict

        try:
            return self._cognitive_state_application().apply_outcome(
                feedback,
                principal=principal,
                source_authority_catalog=source_authority_catalog,
            )
        except (
            FileNotFoundError,
            ValueError,
            sqlite3.Error,
            CognitiveStateSchemaError,
            CognitiveStateConflict,
        ) as exc:
            return self._cognitive_failure_for_exception(exc)

    @staticmethod
    def _cognitive_failure(code: str, exc: BaseException) -> Dict:
        return {
            "success": False,
            "schema_version": "mnemos.cognitive_operation_failure.v1",
            "status": "rejected",
            "error_code": code,
            "message": str(exc),
        }

    @classmethod
    def _cognitive_failure_for_exception(cls, exc: BaseException) -> Dict:
        from core.cognitive.state_schema import CognitiveStateSchemaError
        from core.cognitive.state_store import CognitiveStateConflict

        if isinstance(exc, FileNotFoundError):
            code = "not_initialized"
        elif isinstance(exc, CognitiveStateSchemaError):
            code = "migration_required"
        elif isinstance(exc, CognitiveStateConflict):
            code = "state_conflict"
        elif isinstance(exc, ValueError):
            code = "invalid_request"
        elif isinstance(exc, sqlite3.Error):
            code = "persistence_failed"
        else:
            raise exc
        return cls._cognitive_failure(code, exc)

    def session_search(
        self,
        query: str = "",
        session_id: str = "",
        uid: str = "",
        limit: int = 10,
        days: int | None = None,
        source: str | None = None,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        return self._storage.session_search(
            query=query,
            session_id=session_id,
            uid=uid,
            limit=limit,
            days=days,
            source=source,
            principal=principal,
            narrowing=narrowing,
        )

    def knowledge_ingest(
        self,
        content: str,
        tags: List[str] | None = None,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        return self._storage.knowledge_ingest(
            content,
            tags=tags,
            principal=principal,
        )

    @staticmethod
    def _infer_type_from_path(page_path: str) -> str:
        if "/" in page_path:
            return page_path.split("/")[0]
        return "00-Inbox"

    def wiki_search(
        self,
        query: str,
        limit: int = 5,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> tuple[List[Dict], Dict[str, int]]:
        from core.app.context_search import ContextAwareSearch
        from core.config import get_config

        wiki_dir = get_config().wiki_dir
        if principal is None:
            return [], {"principal_required": 1}
        narrowing = narrowing or AccessNarrowing()
        from core.application.intelligence import IntelligenceApplicationService

        authorized_surface, preflight_summary = (
            IntelligenceApplicationService._authorized_wiki_surface(
                principal,
                narrowing,
            )
        )
        if not authorized_surface:
            return [], preflight_summary
        results: List[Any] = []
        context_results: List[Any] = []
        searcher = None

        try:
            searcher = ContextAwareSearch(wiki_base=str(wiki_dir))
            context_results = searcher.search(
                query,
                limit=limit,
                principal=principal,
                narrowing=narrowing,
            )
            for ca_result in context_results:
                results.append(
                    {
                        "page_id": ca_result.page_path.replace(".md", ""),
                        "title": ca_result.title,
                        "type": self._infer_type_from_path(ca_result.page_path),
                        "heat_level": getattr(ca_result, "heat_level", "cold"),
                        "heat_score": round(float(getattr(ca_result, "heat_score", 0.0) or 0.0), 1),
                        "relevance_score": round(ca_result.relevance, 2),
                        "reasons": [ca_result.match_reason or "context_search"],
                        "matched_terms": list(getattr(ca_result, "matched_terms", []) or []),
                        "score_breakdown": getattr(ca_result, "score_breakdown", {}) or {},
                        "verification": getattr(ca_result, "verification", ""),
                        "confidence": getattr(ca_result, "confidence", 0.5),
                        "source": getattr(ca_result, "source", ""),
                        "scope": getattr(ca_result, "scope", ""),
                        "source_agent": getattr(ca_result, "source_agent", ""),
                        "session_id": getattr(ca_result, "session_id", ""),
                        "project": getattr(ca_result, "project", ""),
                        "tags": list(getattr(ca_result, "tags", []) or []),
                        "acl_schema_version": getattr(
                            ca_result,
                            "acl_schema_version",
                            0,
                        ),
                        "acl_metadata_complete": getattr(
                            ca_result,
                            "acl_metadata_complete",
                            False,
                        ),
                        "acl_reconciliation_status": getattr(
                            ca_result,
                            "acl_reconciliation_status",
                            "",
                        ),
                    }
                )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            self._logger.debug("ContextAwareSearch 失败；拒绝未授权回退", exc_info=True)
        results, access_summary = filter_authorized_items(
            results,
            principal,
            narrowing,
        )
        if not results:
            return [], access_summary

        authorized_page_ids = {str(item.get("page_id", "")) for item in results}
        if searcher is not None and context_results:
            authorized_context_results = [
                result
                for result in context_results
                if result.page_path.removesuffix(".md") in authorized_page_ids
            ]
            authorized_context_results = searcher.rerank_authorized(
                query,
                authorized_context_results,
                limit=limit,
            )
            ordered_page_ids = [
                result.page_path.removesuffix(".md") for result in authorized_context_results
            ]
            results_by_page = {str(item.get("page_id", "")): item for item in results}
            results = [
                results_by_page[page_id]
                for page_id in ordered_page_ids
                if page_id in results_by_page
            ]
            searcher.record_authorized_search(
                query,
                authorized_context_results,
                principal=principal,
                narrowing=narrowing,
            )

        try:
            from datetime import datetime as _dt
            from core.persona.psyche import get_signal_store

            store = get_signal_store()
            timestamp = _dt.now().isoformat()
            for _result in results:
                result = dict(_result) if not isinstance(_result, dict) else _result
                page_id = result.get("page_id", "")
                if page_id:
                    store.insert_knowledge_signal(
                        page_path=page_id,
                        action_type="search",
                        timestamp=timestamp,
                    )
        except ImportError:
            pass

        return results, access_summary

    def wiki_read(
        self,
        page_path: str,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        from core.config import get_config
        from core.frontmatter import read_frontmatter_only

        if principal is None:
            return {
                "success": False,
                "code": "principal_required",
                "path": page_path,
            }
        wiki_root = Path(get_config().wiki_dir).expanduser().resolve()
        normalized = str(page_path or "").replace("\\", "/")
        if not normalized.endswith(".md"):
            normalized += ".md"
        target = (wiki_root / normalized).resolve()
        try:
            relative = target.relative_to(wiki_root).as_posix()
        except ValueError:
            return {
                "success": False,
                "code": "path_traversal_forbidden",
                "message": "页面路径超出 Wiki 目录范围",
                "path": page_path,
            }
        if not target.is_file():
            return {
                "success": False,
                "code": "page_not_found",
                "path": page_path,
            }
        try:
            frontmatter = read_frontmatter_only(target, errors="ignore")
        except (OSError, ValueError):
            return {
                "success": False,
                "code": "acl_metadata_missing",
                "path": page_path,
            }
        access_metadata = {"page_id": relative, "frontmatter": frontmatter}
        decision = authorize_item(
            principal,
            access_metadata,
            narrowing or AccessNarrowing(),
        )
        if not decision.allowed:
            return {
                "success": False,
                "code": "access_denied",
                "reason": decision.reason,
                "path": page_path,
            }
        from integrations.oracle import WikiReader

        reader = WikiReader(str(wiki_root))
        content = reader.read_page(page_path)
        try:
            from core.app.context_search import ContextAwareSearch

            ContextAwareSearch.record_search_click(
                page_path,
                principal=principal,
                narrowing=narrowing,
            )
        except ImportError:
            pass
        try:
            from datetime import datetime as _dt
            from core.persona.psyche import get_signal_store

            get_signal_store().insert_knowledge_signal(
                page_path=page_path,
                action_type="read",
                timestamp=_dt.now().isoformat(),
            )
        except ImportError:
            pass
        return {
            "success": True,
            "content": content,
            "path": page_path,
        }

    def wiki_write(
        self,
        page_path: str,
        content: str,
        frontmatter: Dict | None = None,
        *,
        principal: PrincipalEnvelope | None = None,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        from core.access_policy import complete_write_acl
        from core.application.trusted_write_bridge import write_application_wiki_page
        from core.config import get_config
        from core.frontmatter import read_frontmatter_only

        if principal is not None:
            wiki_root = Path(get_config().wiki_dir).expanduser().resolve()
            normalized = str(page_path or "").replace("\\", "/")
            if not normalized.endswith(".md"):
                normalized += ".md"
            target = (wiki_root / normalized).resolve()
            try:
                relative = target.relative_to(wiki_root).as_posix()
            except ValueError:
                return {
                    "success": False,
                    "code": "path_traversal_forbidden",
                    "message": "页面路径超出 Wiki 目录范围",
                    "path": page_path,
                }
            if target.exists():
                try:
                    frontmatter = read_frontmatter_only(target, errors="ignore")
                except (OSError, ValueError):
                    return {
                        "success": False,
                        "code": "acl_metadata_missing",
                        "path": page_path,
                    }
                access_metadata = {"page_id": relative, "frontmatter": frontmatter}
                existing_owner = item_access_fields(access_metadata)["source_agent"]
                if existing_owner != principal.agent:
                    return {
                        "success": False,
                        "code": "cross_agent_write_forbidden",
                        "path": page_path,
                    }
                decision = authorize_item(
                    principal,
                    access_metadata,
                    AccessNarrowing(session_id=session_id, project=project),
                )
                if not decision.allowed:
                    return {
                        "success": False,
                        "code": decision.reason,
                        "path": page_path,
                    }

        return write_application_wiki_page(
            page_path=page_path,
            content=content,
            frontmatter=complete_write_acl(
                frontmatter,
                principal=principal,
                session_id=session_id,
                project=project,
                page_path=page_path,
            ),
            logger=self._logger,
            principal=principal,
            session_id=session_id,
            project=project,
        )

    def knowledge_source_list(self) -> Dict:
        from core.config import get_config

        wiki_dir = get_config().wiki_dir

        sources = {
            "human_written": 0,
            "l1_sync": 0,
            "distilled": 0,
            "retrospective": 0,
            "git_knowledge": 0,
            "other": 0,
        }

        if not wiki_dir.exists():
            return {"success": True, "sources": sources, "total": 0}

        import yaml

        max_file_size = 1 * 1024 * 1024
        for md_file in wiki_dir.rglob("*.md"):
            try:
                if md_file.stat().st_size > max_file_size:
                    self._logger.warning("Wiki 文件过大跳过: %s", md_file.name)
                    continue
                content = md_file.read_text(encoding="utf-8", errors="ignore")
                src = "human_written"
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            frontmatter = yaml.safe_load(parts[1]) or {}
                            from core.frontmatter import fm_get

                            tags = frontmatter.get("tags", [])
                            if "l1-sync" in tags or "l1-storage" in tags:
                                src = "l1_sync"
                            elif "distilled" in tags:
                                src = "distilled"
                            elif "retrospective" in tags:
                                src = "retrospective"
                            elif "git" in tags:
                                src = "git_knowledge"
                            explicit = fm_get(frontmatter, "source", "")
                            if explicit in ("l1", "l1-sync"):
                                src = "l1_sync"
                            elif explicit in ("distill", "distilled"):
                                src = "distilled"
                            elif explicit == "retrospective":
                                src = "retrospective"
                            elif explicit == "git":
                                src = "git_knowledge"
                            elif explicit in ("claude", "kimi", "codex", "openclaw", "hermes"):
                                src = "distilled"
                        except ImportError:
                            self._logger.warning("Caught unexpected error", exc_info=True)
                sources[src] = sources.get(src, 0) + 1
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                self._logger.warning("Caught unexpected error at agora.py", exc_info=True)
                continue

        total = sum(sources.values())
        return {
            "success": True,
            "sources": sources,
            "total": total,
            "wiki_dir": str(wiki_dir),
        }

    def knowledge_distill(
        self,
        session_id: str,
        messages: List[Dict],
        write_to_wiki: bool = True,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        try:
            import hashlib
            from datetime import datetime
            from core.sync_framework.capture_service import CaptureService

            if messages:
                wd = os.getcwd()
                sid = session_id
                if not sid:
                    dir_hash = hashlib.md5(wd.encode(), usedforsecurity=False).hexdigest()[:8]
                    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                    sid = f"mcp:{dir_hash}:{ts}"

                capture = CaptureService(start_worker=False)
                completeness = {
                    "visible_text": "host_provided",
                    "loss_reasons": ["host_session_messages_may_be_compressed"],
                }
                capture.capture_session(
                    source_agent=principal.agent,
                    session_id=sid,
                    turns=[
                        {
                            **turn,
                            "cwd": wd,
                            "metadata": {"capture_source": "mcp_tool"},
                            "completeness": completeness,
                        }
                        for turn in self._messages_to_turns(messages)
                    ],
                )
                capture.end_session(principal.agent, sid)
                try:
                    worker_pool = getattr(capture, "worker_pool", None)
                    if worker_pool is not None:
                        worker_pool.flush_session(principal.agent, sid)
                except APPLICATION_OPERATION_ERRORS as exc:
                    self._logger.warning("mcp flush_session failed: %s", exc, exc_info=True)

            return {
                "success": True,
                "message": "蒸馏任务已入队，由 HephaestusWorker 通过 LLM API 异步蒸馏",
                "session_id": session_id,
                "note": (
                    "任务进入 CaptureService → SyncEngine → amphora → "
                    "HephaestusWorker → LLM API → Wiki"
                ),
            }
        except APPLICATION_OPERATION_ERRORS as exc:
            self._logger.error("蒸馏入队失败: %s", exc)
            return {
                "success": False,
                "message": f"蒸馏入队失败: {exc}",
            }

    def document_process(
        self,
        file_path: str,
        title: str = "",
        mode: str = "distill",
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        from core.application.document_import_service import DocumentImportService

        try:
            result = DocumentImportService().import_document(
                file_path,
                mode=mode,
                title=title,
                agent_name=principal.agent,
            )
            parse_result = result.get("parse_result", {})
            if isinstance(parse_result, dict):
                for key, value in parse_result.items():
                    if key != "status":
                        result.setdefault(key, value)
            result["requires_api_key"] = mode == "distill"
            result["api_mode"] = {
                "distill": "capture_outbox_distillation",
                "capture": "canonical_raw_capture",
                "parse": "parse_only",
                "watch": "daemon_watch_preflight",
            }.get(mode, mode)
            return result
        except APPLICATION_OPERATION_ERRORS as exc:
            self._logger.error("文档处理失败: %s", exc)
            return {
                "success": False,
                "message": f"处理失败: {exc}",
            }

    def wiki_build(self, dry_run: bool = False) -> Dict:
        from core.hephaestus.wiki_builder import run_build_cycle

        try:
            backend = self.storage_backend()
            result = run_build_cycle(backend, dry_run=dry_run)
            return {
                "success": True,
                "message": "Wiki 回追构建完成",
                "dry_run": dry_run,
                "result": result,
            }
        except APPLICATION_OPERATION_ERRORS as exc:
            self._logger.error("Wiki 回追构建失败: %s", exc)
            return {
                "success": False,
                "message": f"构建失败: {exc}",
            }

    def preflight_inject(
        self,
        task_type: str,
        subtype: str = "",
        context_text: str = "",
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        return self._kia.preflight_inject(
            task_type,
            subtype,
            context_text,
            principal=principal,
            narrowing=narrowing,
        )

    def check_pending_recaps(self, user_context: Dict | None = None, limit: int = 5) -> Dict:
        return self._kia.check_pending_recaps(user_context=user_context, limit=limit)

    def recap_start(
        self,
        task_id: str = "",
        topic: str = "",
        mode: str = "minimal",
        source_agent: str = "",
        owner_agent: str = "",
        source_agents: List[str] | None = None,
        session_id: str = "",
        context: Dict | None = None,
        project: str = "",
        task_type: str = "",
        subtype: str = "",
    ) -> Dict:
        return self._kia.recap_start(
            task_id=task_id,
            topic=topic,
            mode=mode,
            source_agent=source_agent,
            owner_agent=owner_agent,
            source_agents=source_agents,
            session_id=session_id,
            context=context,
            project=project,
            task_type=task_type,
            subtype=subtype,
        )

    def recap_submit(
        self,
        recap_id: str,
        answers: Dict,
        confirm_level: str = "draft",
        source_agent: str = "",
    ) -> Dict:
        return self._kia.recap_submit(
            recap_id=recap_id,
            answers=answers,
            confirm_level=confirm_level,
            source_agent=source_agent,
        )

    def recap_finalize(
        self,
        recap_id: str,
        write_policy: str = "save_and_index",
        follow_up_at: str = "",
        confirmed_by_user: bool = True,
        source_agent: str = "",
    ) -> Dict:
        return self._kia.recap_finalize(
            recap_id=recap_id,
            write_policy=write_policy,
            follow_up_at=follow_up_at,
            confirmed_by_user=confirmed_by_user,
            source_agent=source_agent,
        )

    def recap_skip(
        self,
        recap_id: str = "",
        task_id: str = "",
        skip_reason: str = "",
        user_note: str = "",
        owner_agent: str = "",
        source_agent: str = "",
    ) -> Dict:
        return self._kia.recap_skip(
            recap_id=recap_id,
            task_id=task_id,
            skip_reason=skip_reason,
            user_note=user_note,
            owner_agent=owner_agent,
            source_agent=source_agent,
        )

    def recap_feedback(
        self,
        recap_id: str,
        feedback_type: str,
        comment: str = "",
        source_agent: str = "",
        supersedes_event_id: str = "",
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        """Record authenticated recap feedback through canonical attribution."""

        return self._kia.recap_feedback(
            recap_id=recap_id,
            feedback_type=feedback_type,
            comment=comment,
            source_agent=source_agent,
            supersedes_event_id=supersedes_event_id,
            principal=principal,
            narrowing=narrowing,
        )

    def recap_status(
        self,
        recap_id: str = "",
        task_id: str = "",
        source_agent: str = "",
    ) -> Dict:
        return self._kia.recap_status(
            recap_id=recap_id,
            task_id=task_id,
            source_agent=source_agent,
        )

    def recap_claim_owner(
        self,
        recap_id: str,
        owner_agent: str,
        current_session_id: str = "",
    ) -> Dict:
        return self._kia.recap_claim_owner(
            recap_id=recap_id,
            owner_agent=owner_agent,
            current_session_id=current_session_id,
        )

    def guard_check(
        self,
        user_message: str,
        ai_response: str = "",
        task_type: str = "",
        subtype: str = "",
        context: Dict | None = None,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        return self._kia.guard_check(
            user_message=user_message,
            ai_response=ai_response,
            task_type=task_type,
            subtype=subtype,
            context=context,
            principal=principal,
            narrowing=narrowing,
        )

    def retrospective_list(
        self,
        task_type: str | None = None,
        limit: int = 10,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        return self._kia.retrospective_list(
            task_type=task_type,
            limit=limit,
            principal=principal,
            narrowing=narrowing,
        )

    def persona_summary(
        self,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        return self._persona.persona_summary(
            principal=principal,
            narrowing=narrowing,
        )

    def persona_behavior_prompt(
        self,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        return self._persona.persona_behavior_prompt(
            principal=principal,
            narrowing=narrowing,
        )

    def persona_behavior_metrics(
        self,
        days: int = 30,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        return self._persona.persona_behavior_metrics(
            days=days,
            principal=principal,
            narrowing=narrowing,
        )

    def record_explicit_profile_evidence(
        self,
        request: Mapping[str, Any],
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        source_authority_catalog: "SourceAuthorityCatalog",
    ) -> Dict:
        """Append a Profile v2 signal only from a verified explicit-user span."""

        from core.config import get_config

        source_authority_id = str(request.get("source_authority_id") or "").strip()
        config = get_config()
        configured_raw_db = str(config.get("raw_event_store.db_path", None) or "")
        raw_db_path = Path(configured_raw_db).expanduser() if configured_raw_db else (
            Path(config.database_dir) / "raw_events.db"
        )
        try:
            result = self._persona.record_explicit_profile_evidence(
                source_authority_catalog=source_authority_catalog,
                source_authority_id=source_authority_id,
                raw_db_path=raw_db_path,
                principal=principal,
                narrowing=narrowing,
                signal_type=str(request.get("signal_type") or ""),
                dimension=str(request.get("dimension") or ""),
                quote=str(request.get("quote") or ""),
                confidence=float(request.get("confidence", 0.5)),
                assertion_id=str(request.get("assertion_id") or ""),
                expected_revision_id=str(request.get("expected_revision_id") or ""),
            )
        except (FileNotFoundError, PermissionError, ValueError, OSError, sqlite3.Error) as exc:
            return self._cognitive_failure("profile_evidence_rejected", exc)
        return {
            "success": True,
            "schema_version": "mnemos.persona_profile_evidence.v1",
            "status": "recorded",
            **result,
        }

    def load_onboarding_prompt(self) -> str:
        return self._persona.load_onboarding_prompt()

    def signal_collect(self, sources: List[str] | None = None) -> Dict:
        return self._persona.signal_collect(sources=sources)

    def persona_update(
        self,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        return self._persona.persona_update(
            principal=principal,
            narrowing=narrowing,
        )

    def context_aware_search(
        self,
        query: str,
        limit: int = 10,
        working_dir: str = "",
        session_id: str = "",
        project: str = "",
        *,
        principal: PrincipalEnvelope | None = None,
    ) -> Dict:
        if principal is None:
            return {
                "success": False,
                "code": "principal_required",
                "results": [],
                "count": 0,
            }
        return self._intelligence.context_aware_search(
            query=query,
            limit=limit,
            working_dir=working_dir,
            principal=principal,
            narrowing=AccessNarrowing(
                session_id=session_id,
                project=project,
            ),
        )

    def intent_route(self, user_input: str, working_dir: str = "") -> Dict:
        return self._intelligence.intent_route(user_input, working_dir)

    def intent_correct(self, user_input: str, original_intent: str, corrected_intent: str) -> Dict:
        return self._intelligence.intent_correct(user_input, original_intent, corrected_intent)

    def blindspot_check(
        self,
        query: str,
        session_id: str = "",
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        return self._intelligence.blindspot_check(
            query,
            session_id=session_id,
            principal=principal,
            narrowing=narrowing,
        )

    def predictive_push(
        self,
        user_input: str,
        working_dir: str = "",
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        return self._intelligence.predictive_push(
            user_input,
            working_dir,
            principal=principal,
            narrowing=narrowing,
        )

    def push_feedback(
        self,
        topic: str,
        action: str,
        delivery_event_id: str,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
        supersedes_event_id: str = "",
        correction_target_ref: str = "",
        correction_reason: str = "",
    ) -> Dict:
        return self._intelligence.push_feedback(
            topic,
            action,
            delivery_event_id,
            principal=principal,
            narrowing=narrowing,
            supersedes_event_id=supersedes_event_id,
            correction_target_ref=correction_target_ref,
            correction_reason=correction_reason,
        )

    def record_delivery_display(
        self,
        delivery_event_id: str,
        rendered_content_hash: str,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        """Record a host-bound presentation receipt through the sole delivery owner."""
        from core.cognitive.delivery_router import KnowledgeDeliveryRouter

        return KnowledgeDeliveryRouter().record_presentation(
            delivery_event_id,
            host_agent=principal.agent,
            rendered_content_hash=rendered_content_hash,
        )

    def freshness_check(
        self,
        entity_name: str,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        return self._intelligence.freshness_check(
            entity_name,
            principal=principal,
            narrowing=narrowing,
        )

    def observation_run(self, full: bool = False, since: str = "") -> Dict:
        return self._observation.observation_run(full=full, since=since)

    def observation_search(
        self,
        dimension: str = "",
        source_type: str = "",
        limit: int = 20,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = "observation_read",
    ) -> Dict:
        return self._observation.observation_search(
            dimension=dimension,
            source_type=source_type,
            limit=limit,
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
        )

    def build_reflect_tool_result(self, result, route) -> Dict:
        return self._reflection.build_reflect_tool_result(result, route)

    def get_reflection_engine(self, use_llm: bool = True):
        return self._reflection.get_reflection_engine(use_llm=use_llm)

    def reflect_on_input(
        self,
        text: str,
        auto_llm: bool = True,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        return self._reflection.reflect_on_input(
            text,
            auto_llm=auto_llm,
            principal=principal,
            narrowing=narrowing,
        )

    def reflect_manually(
        self,
        query: str = "",
        auto_llm: bool = True,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        return self._reflection.reflect_manually(
            query,
            auto_llm=auto_llm,
            principal=principal,
            narrowing=narrowing,
        )

    def reflection_feedback(
        self,
        reflection_id: str,
        feedback_type: str,
        comment: str = "",
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        supersedes_event_id: str = "",
        correction_target_ref: str = "",
        correction_reason: str = "",
    ) -> Dict:
        return self._reflection.reflection_feedback(
            reflection_id,
            feedback_type,
            comment,
            principal=principal,
            narrowing=narrowing,
            supersedes_event_id=supersedes_event_id,
            correction_target_ref=correction_target_ref,
            correction_reason=correction_reason,
        )

    def reflection_pending(
        self,
        hours_since: float = 24,
        limit: int = 20,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        return self._reflection.reflection_pending(
            hours_since=hours_since,
            limit=limit,
            principal=principal,
            narrowing=narrowing,
        )

    def infer_type_from_path(self, page_path: str) -> str:
        return self._memory.infer_type_from_path(page_path)

    def scope_slug(self, value: str) -> str:
        return self._memory.scope_slug(value)

    def scope_page_path(
        self,
        scope: str,
        title: str,
        page_path: str = "",
        scope_name: str = "",
    ) -> str:
        return self._memory.scope_page_path(scope, title, page_path, scope_name)

    def memory_write_project(
        self,
        title: str,
        content: str,
        project: str = "",
        page_path: str = "",
        frontmatter: Dict | None = None,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        return self._memory.memory_write_project(
            title,
            content,
            project=project,
            page_path=page_path,
            frontmatter=frontmatter,
            principal=principal,
        )

    def memory_write_framework(
        self,
        title: str,
        content: str,
        framework: str = "",
        page_path: str = "",
        frontmatter: Dict | None = None,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        return self._memory.memory_write_framework(
            title,
            content,
            framework=framework,
            page_path=page_path,
            frontmatter=frontmatter,
            principal=principal,
        )

    def memory_write_global(
        self,
        title: str,
        content: str,
        page_path: str = "",
        frontmatter: Dict | None = None,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        return self._memory.memory_write_global(
            title,
            content,
            page_path=page_path,
            frontmatter=frontmatter,
            principal=principal,
        )

    def memory_search(
        self,
        query: str,
        scope: str = "all",
        limit: int = 5,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        return self._memory.memory_search(
            query,
            scope=scope,
            limit=limit,
            principal=principal,
            narrowing=narrowing,
        )

    def self_diagnose(self) -> Dict:
        from core.diagnostics import ConnectionDiagnostics

        report = ConnectionDiagnostics.full_report()
        report["success"] = True
        return report

    def configure_wiki(self, vault_path: str) -> Dict:
        from core.config import get_config

        config = get_config()
        try:
            path = Path(vault_path).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)

            config.set("wiki.vault_path", str(path))
            config.set("vaults.mnemos.path", str(path))
            config.save()

            return {
                "success": True,
                "vault_path": str(path),
                "exists": path.exists(),
                "writable": os.access(path, os.W_OK),
                "message": f"Wiki 路径已配置: {path}",
            }
        except APPLICATION_OPERATION_ERRORS as exc:
            self._logger.error("配置 Wiki 失败: %s", exc, exc_info=True)
            return {"success": False, "message": f"配置失败: {exc}"}

    def detect_sources(self) -> Dict:
        from core.diagnostics import ConnectionDiagnostics

        agents = ConnectionDiagnostics.check_agents()
        wiki = ConnectionDiagnostics.check_wiki()
        storage = ConnectionDiagnostics.check_storage()

        sources = {
            "storage": {
                "backend": storage.backend,
                "configured": storage.configured,
                "reachable": storage.reachable,
                "path": storage.path,
            },
            "wiki": {
                "path": wiki.path,
                "exists": wiki.exists,
                "writable": wiki.writable,
            },
        }

        for agent in agents:
            sources[agent.name] = {
                "detected": agent.available,
                "path": agent.data_dir,
                "hooks_installed": agent.hooks_installed,
            }

        from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
        from core.sync_framework.registry import PathDiscover

        support_manifest = get_agent_source_support_manifest()
        for name in support_manifest.active_source_names:
            if name not in sources:
                data_dir = PathDiscover.find(name)
                sources[name] = {
                    "detected": data_dir is not None,
                    "path": str(data_dir) if data_dir else None,
                    "hooks_installed": False,
                    "source_role": support_manifest.source(name).role,
                }

        return {
            "success": True,
            "sources": sources,
        }


# Backward-compatible public name used by older docs, snippets, and agent checks.
ApplicationFacade = DefaultMnemosServiceFacade
