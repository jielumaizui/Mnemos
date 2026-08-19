# -*- coding: utf-8 -*-
"""KIA application service used by the integration facade."""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict

from core.access_policy import AccessNarrowing, PrincipalEnvelope, authorize_item

AGENT_POLICY_REMINDER = (
    "【Mnemos Agent 规范】当用户给出文件路径并要求'蒸馏/导入知识库'时，"
    "必须调用 document_process MCP tool。此场景下禁止自行读取文件并蒸馏、"
    "禁止自行调用 API、禁止直接展示未经管道的处理结果。"
    "正常的代码编辑/审查/调试场景，Agent 可自行读取文件。"
    "详见项目根目录 AGENT_BEHAVIOR_POLICY.md"
)


class KiaApplicationService:
    """Default implementation for KIA-facing facade operations."""

    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger(__name__)
        self._guard_sessions: OrderedDict[str, tuple[Any, datetime]] = OrderedDict()
        self._guard_session_ttl_seconds = 86400
        self._guard_sessions_max = 1000
        self._guard_knowledge_cache: OrderedDict[str, tuple[Any, datetime]] = OrderedDict()
        self._guard_cache_ttl_seconds = 300
        self._guard_knowledge_cache_max = 256

    def _prune_guard_knowledge_cache(self) -> None:
        now = datetime.now()
        ttl = timedelta(seconds=self._guard_cache_ttl_seconds)
        expired = [
            key
            for key, (_, loaded_at) in self._guard_knowledge_cache.items()
            if now - loaded_at >= ttl
        ]
        for key in expired:
            del self._guard_knowledge_cache[key]
        while len(self._guard_knowledge_cache) > self._guard_knowledge_cache_max:
            self._guard_knowledge_cache.popitem(last=False)

    def _prune_guard_sessions(self) -> None:
        now = datetime.now()
        ttl = timedelta(seconds=self._guard_session_ttl_seconds)
        expired = []
        for key, (guard, created_at) in self._guard_sessions.items():
            if now - created_at >= ttl:
                expired.append(key)
                try:
                    if hasattr(guard, "close"):
                        guard.close()
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    ImportError,
                    AttributeError,
                    RuntimeError,
                ):
                    self._logger.debug("关闭过期 guard session 失败", exc_info=True)
        for key in expired:
            del self._guard_sessions[key]
        while len(self._guard_sessions) > self._guard_sessions_max:
            _, (guard, _) = self._guard_sessions.popitem(last=False)
            try:
                if hasattr(guard, "close"):
                    guard.close()
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                self._logger.debug("关闭超限 guard session 失败", exc_info=True)

    def _route_delivery_event(
        self,
        *,
        source: str,
        subject: str,
        channel: str,
        target: str = "",
        evidence_refs: list[str] | None = None,
        task_fit_score: float = 0.8,
        requested_level: str = "hint",
        task_key: str = "",
        cooldown_key: str = "",
        active_risk: bool = False,
        metadata: Dict | None = None,
    ) -> Dict:
        """Record a KIA delivery decision without making the tool call fragile."""
        try:
            from core.cognitive.delivery_router import KnowledgeDeliveryRouter

            decision = KnowledgeDeliveryRouter().route_candidate(
                source=source,
                subject=subject,
                channel=channel,
                target=target,
                evidence_refs=evidence_refs or [],
                task_fit_score=task_fit_score,
                requested_level=requested_level,
                task_key=task_key,
                cooldown_key=cooldown_key,
                active_risk=active_risk,
                metadata=metadata or {},
            )
            return {
                "delivery_event_id": decision.event_id,
                "delivery_decision": decision.to_dict(),
            }
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ) as exc:
            self._logger.debug(
                "[kia] delivery routing failed for %s/%s: %s",
                source,
                channel,
                exc,
                exc_info=True,
            )
            return {}

    def _active_policy_patches(
        self,
        task_type: str = "",
        subtype: str = "",
        context_text: str = "",
        scope: str = "global",
    ) -> list[Any]:
        """Load bounded policy patches for preflight/guard only."""
        try:
            from core.cognitive.policy_patch import PolicyPatchStore

            return PolicyPatchStore().active_for(
                task_type=task_type or "general",
                subtype=subtype or "general",
                context=context_text,
                scope=scope or "global",
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ) as exc:
            self._logger.debug(
                "[kia] policy patch lookup failed for %s/%s: %s",
                task_type,
                subtype,
                exc,
                exc_info=True,
            )
            return []

    @staticmethod
    def _policy_patch_checklist_items(policy_patches: list[Any]) -> list[Any]:
        """Convert policy patches into regular KIA checklist items."""
        from core.kia.policy_patch_adapter import to_checklist_items

        return to_checklist_items(policy_patches)

    @staticmethod
    def _policy_patch_dicts(policy_patches: list[Any]) -> list[Dict]:
        from core.kia.policy_patch_adapter import to_response_dicts

        return to_response_dicts(policy_patches)

    def preflight_inject(
        self,
        task_type: str,
        subtype: str = "",
        context_text: str = "",
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Load KIA preflight knowledge and fall back to general wiki search."""
        if principal is None:
            # Preflight text is prompt-bound knowledge.  It must not construct
            # an injector, cache, or generic Wiki fallback before the server
            # has resolved a read principal.
            return {
                "success": True,
                "loaded": False,
                "source": "access_denied",
                "task_type": task_type,
                "subtype": subtype,
                "checklist_count": 0,
                "checklist": [],
                "policy_patches": [],
                "access_filter": {"principal_required": 1},
                "agent_policy_reminder": AGENT_POLICY_REMINDER,
            }
        degraded_reason = ""
        from core.application.intelligence import IntelligenceApplicationService

        fallback_query = " ".join(
            part for part in [task_type, subtype, context_text] if part
        ).strip()
        search_response = IntelligenceApplicationService(self._logger).context_aware_search(
            fallback_query,
            limit=3,
            principal=principal,
            narrowing=narrowing or AccessNarrowing(),
        )
        authorized_fallback_results: list[Dict] = list(search_response.get("results", []))
        knowledge = None
        policy_patches = self._active_policy_patches(
            task_type,
            subtype,
            context_text,
            scope=(narrowing.project if narrowing and narrowing.project else "global"),
        )
        policy_patch_items = self._policy_patch_checklist_items(policy_patches)
        policy_patch_dicts = self._policy_patch_dicts(policy_patches)

        def with_degraded(response: Dict) -> Dict:
            if degraded_reason:
                response["degraded"] = True
                response["degraded_reason"] = degraded_reason
            return response

        if not knowledge:
            fallback_query = " ".join(
                part for part in [task_type, subtype, context_text] if part
            ).strip()
            fallback_results: list[Any] = authorized_fallback_results or []
            if fallback_results:
                response = {
                    "success": True,
                    "loaded": True,
                    "source": "general_wiki_fallback",
                    "message": f"未找到 {task_type}/{subtype} 的 retrospective，已回退装载通用知识",
                    "task_type": task_type,
                    "subtype": subtype,
                    "checklist_count": len(policy_patch_items),
                    "checklist": [
                        {
                            "item": item.item,
                            "source": item.source,
                            "severity": item.severity,
                            "freshness_score": round(item.freshness_score, 2),
                            "hit_count": item.hit_count,
                            **({"detail": item.detail} if item.detail else {}),
                        }
                        for item in policy_patch_items
                    ],
                    "lessons_summary": "未命中专用复盘经验，以下为相关 Wiki 知识。",
                    "policy_patches": policy_patch_dicts,
                    "knowledge_results": [
                        {
                            "page_path": (
                                result.get("page_path", "")
                                if isinstance(result, dict)
                                else result.page_path
                            ),
                            "title": (
                                result.get("title", "")
                                if isinstance(result, dict)
                                else result.title
                            ),
                            "snippet": (
                                result.get("snippet", "")
                                if isinstance(result, dict)
                                else result.snippet
                            ),
                            "score": round(
                                float(
                                    result.get("score", 0.0)
                                    if isinstance(result, dict)
                                    else result.score
                                ),
                                3,
                            ),
                        }
                        for result in fallback_results
                    ],
                    "agent_policy_reminder": AGENT_POLICY_REMINDER,
                }
                response.update(
                    self._route_delivery_event(
                        source="preflight_inject",
                        subject=f"{task_type}:{subtype or 'general'}",
                        channel="preflight_inject",
                        target=(
                            fallback_results[0].get("page_path", "")
                            if isinstance(fallback_results[0], dict)
                            else fallback_results[0].page_path
                        ),
                        evidence_refs=[
                            (
                                result.get("page_path", "")
                                if isinstance(result, dict)
                                else result.page_path
                            )
                            for result in fallback_results
                        ],
                        task_fit_score=0.8,
                        requested_level="silent",
                        task_key=task_type or "preflight",
                        cooldown_key=f"preflight:{task_type}:{subtype or 'general'}",
                        metadata={
                            "mode": "general_wiki_fallback",
                            "result_count": len(fallback_results),
                            "policy_patch_count": len(policy_patches),
                        },
                    )
                )
                return with_degraded(response)

            if policy_patches:
                response = {
                    "success": True,
                    "loaded": True,
                    "source": "policy_patch",
                    "message": f"未找到 {task_type}/{subtype} 的 retrospective，已装载策略补丁",
                    "task_type": task_type,
                    "subtype": subtype,
                    "checklist_count": len(policy_patch_items),
                    "checklist": [
                        {
                            "item": item.item,
                            "source": item.source,
                            "severity": item.severity,
                            "freshness_score": round(item.freshness_score, 2),
                            "hit_count": item.hit_count,
                            **({"detail": item.detail} if item.detail else {}),
                        }
                        for item in policy_patch_items
                    ],
                    "lessons_summary": "未命中专用复盘经验，已从策略补丁装载受控清单。",
                    "policy_patches": policy_patch_dicts,
                    "agent_policy_reminder": AGENT_POLICY_REMINDER,
                }
                first_patch = policy_patches[0]
                response.update(
                    self._route_delivery_event(
                        source="preflight_inject",
                        subject=getattr(first_patch, "content", ""),
                        channel="preflight_inject",
                        target=getattr(first_patch, "patch_id", ""),
                        evidence_refs=[
                            ref
                            for patch in policy_patches
                            for ref in (getattr(patch, "evidence_refs", []) or [])
                        ],
                        task_fit_score=0.85,
                        requested_level="silent",
                        task_key=task_type or "preflight",
                        cooldown_key=f"policy_patch:{getattr(first_patch, 'patch_id', '')}",
                        metadata={
                            "mode": "policy_patch",
                            "policy_patch_count": len(policy_patches),
                        },
                    )
                )
                return with_degraded(response)

            return with_degraded(
                {
                    "success": True,
                    "loaded": False,
                    "message": f"未找到 {task_type}/{subtype} 的历史经验",
                    "task_type": task_type,
                    "subtype": subtype,
                    "checklist_count": 0,
                    "checklist": [],
                    "policy_patches": [],
                    "agent_policy_reminder": AGENT_POLICY_REMINDER,
                }
            )

        checklist = list(knowledge.checklist) + policy_patch_items
        response = {
            "success": True,
            "loaded": True,
            "task_type": knowledge.task_type,
            "subtype": knowledge.subtype,
            "version": knowledge.version,
            "checklist_count": len(checklist),
            "checklist": [
                {
                    "item": item.item,
                    "source": item.source,
                    "severity": item.severity,
                    "freshness_score": round(item.freshness_score, 2),
                    "hit_count": item.hit_count,
                    **({"detail": item.detail} if item.detail else {}),
                }
                for item in checklist
            ],
            "lessons_summary": knowledge.lessons_summary,
            "policy_patches": policy_patch_dicts,
            "agent_policy_reminder": AGENT_POLICY_REMINDER,
        }
        response.update(
            self._route_delivery_event(
                source="preflight_inject",
                subject=f"{knowledge.task_type}:{knowledge.subtype or 'general'}",
                channel="preflight_inject",
                evidence_refs=[item.source for item in checklist if item.source],
                task_fit_score=0.85,
                requested_level="silent",
                task_key=knowledge.task_type or "preflight",
                cooldown_key=f"preflight:{knowledge.task_type}:{knowledge.subtype or 'general'}",
                metadata={
                    "mode": "retrospective",
                    "checklist_count": len(checklist),
                    "policy_patch_count": len(policy_patches),
                    "version": knowledge.version,
                },
            )
        )
        return with_degraded(response)

    def retrospective_list(
        self,
        task_type: str | None = None,
        limit: int = 10,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        """List available retrospective knowledge files."""
        from core.config import get_config
        from core.frontmatter import fm_get, normalize_frontmatter, read_frontmatter_only

        wiki_dir = get_config().wiki_dir
        retro_dir = None
        for candidate in [wiki_dir / "06-Retrospectives", wiki_dir / "retrospectives"]:
            if candidate.exists():
                retro_dir = candidate
                break
        if not retro_dir:
            return {"success": True, "retrospectives": []}

        items = []
        access_summary: Dict[str, int] = {}
        max_file_size = 1 * 1024 * 1024
        for md_file in sorted(retro_dir.rglob("*.md"), reverse=True):
            try:
                if md_file.stat().st_size > max_file_size:
                    self._logger.warning("Retrospective 文件过大跳过: %s", md_file.name)
                    continue
                frontmatter = normalize_frontmatter(read_frontmatter_only(md_file, errors="ignore"))
                page_id = str(md_file.relative_to(wiki_dir).with_suffix(""))
                decision = authorize_item(
                    principal,
                    {"page_id": page_id, "frontmatter": frontmatter},
                    narrowing,
                )
                access_summary[decision.reason] = access_summary.get(decision.reason, 0) + 1
                if not decision.allowed:
                    continue
                title = str(fm_get(frontmatter, "title") or md_file.stem)
                applies_when = frontmatter.get("applies_when") or {}
                task_types = (
                    applies_when.get("task_type", []) if isinstance(applies_when, dict) else []
                )
                if task_type and task_type not in task_types:
                    continue

                rel_parts = md_file.relative_to(retro_dir).parts
                item_task_type = rel_parts[0] if len(rel_parts) > 1 else ""
                version_match = re.search(r"(.+)-v(\d+)\.md$", md_file.name)
                if version_match:
                    item_subtype = version_match.group(1)
                    item_version = int(version_match.group(2))
                else:
                    item_subtype = md_file.stem
                    item_version = None

                items.append(
                    {
                        "path": str(md_file.relative_to(retro_dir)),
                        "title": title,
                        "task_type": item_task_type,
                        "subtype": item_subtype,
                        "version": item_version,
                    }
                )
                if len(items) >= limit:
                    break
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self._logger.warning("遍历文件失败: %s", exc)
                reason = "acl_metadata_missing"
                access_summary[reason] = access_summary.get(reason, 0) + 1
                continue

        return {
            "success": True,
            "retrospectives": items,
            "access_filter": access_summary,
        }

    def check_pending_recaps(self, user_context: Dict | None = None, limit: int = 5) -> Dict:
        """Check pending retrospective reminders."""
        from core.app.forced_retrospective import ForcedRetrospective

        user_context = user_context or {}
        forced = ForcedRetrospective()
        items = []

        recaps = forced.get_pending_system_recaps()
        try:
            recaps.extend(forced.list_user_reminders())
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            self._logger.debug("读取用户复盘提醒失败", exc_info=True)

        for recap in recaps[: max(1, int(limit or 5))]:
            decision = forced.should_force_open(recap, user_context)
            item = {
                "task_id": recap.task_id,
                "topic": recap.topic,
                "source": recap.source,
                "severity": recap.severity,
                "status": recap.status,
                "target_page": recap.target_page,
                "age_days": recap.age_days,
                "same_type_count": recap.same_type_count,
                "should_force_open": decision.should_force_open,
                "score": decision.score,
                "reasons": [
                    reason
                    for reason in decision.reason.split("; ")
                    if reason and reason != "no signals"
                ],
                "channel": decision.channel,
            }
            requested_level = (
                "force_open"
                if decision.should_force_open
                else (
                    "warn"
                    if recap.severity in {"critical", "high"} or decision.score >= 3
                    else "hint"
                )
            )
            delivery = self._route_delivery_event(
                source="check_pending_recaps",
                subject=recap.topic or recap.task_id,
                channel=decision.channel or "dialog_reminder",
                target=recap.target_page,
                evidence_refs=[recap.target_page] if recap.target_page else [],
                task_fit_score=min(1.0, 0.55 + max(0, int(decision.score)) * 0.1),
                requested_level=requested_level,
                task_key="check_pending_recaps",
                cooldown_key=recap.task_id,
                active_risk=bool(decision.should_force_open),
                metadata={
                    "task_id": recap.task_id,
                    "severity": recap.severity,
                    "age_days": recap.age_days,
                    "same_type_count": recap.same_type_count,
                    "reasons": item["reasons"],
                },
            )
            if delivery:
                item.update(delivery)
            items.append(item)

        return {
            "success": True,
            "pending_count": len(recaps),
            "items": items,
            "instruction": (
                "宿主 Agent 应在会话开始或任务收尾前调用本工具；"
                "should_force_open=true 时优先提醒用户处理复盘。"
            ),
        }

    def recap_start(
        self,
        task_id: str = "",
        topic: str = "",
        mode: str = "minimal",
        source_agent: str = "",
        owner_agent: str = "",
        source_agents: list | None = None,
        session_id: str = "",
        context: Dict | None = None,
        project: str = "",
        task_type: str = "",
        subtype: str = "",
    ) -> Dict:
        """Start a structured retrospective session."""
        from core.app.retrospective_models import RECAP_QUESTIONS
        from core.app.retrospective_session_manager import RetrospectiveSessionManager

        manager = RetrospectiveSessionManager()
        session = manager.start(
            task_id=task_id,
            owner_agent=owner_agent or source_agent,
            mode=mode,
            topic=topic,
            source_agent=source_agent,
            source_agents=source_agents,
            session_id=session_id,
            context=context,
            project=project,
            task_type=task_type,
            subtype=subtype,
        )
        requested_owner = owner_agent or source_agent
        if requested_owner and session.owner_agent and session.owner_agent != requested_owner:
            return {
                "success": False,
                "error": "owner_conflict",
                "recap_id": session.recap_id,
                "task_id": session.task_id,
                "state": session.state,
                "owner_agent": session.owner_agent,
                "source_agents": session.source_agents,
                "questions": [],
                "message": "已有其他 Agent 负责该复盘，请先调用 recap_claim_owner 或交由 owner 继续。",
            }
        task = manager.forced.get_recap_task(session.task_id)
        return {
            "success": True,
            "recap_id": session.recap_id,
            "task_id": session.task_id,
            "state": session.state,
            "owner_agent": session.owner_agent,
            "source_agents": session.source_agents,
            "questions": RECAP_QUESTIONS,
            "evidence_summary": task.context if task else "",
            "suggested_goal": "",
            "suggested_actual": "",
            "interaction_contract": {
                "must_ask_exactly_three_questions": True,
                "must_confirm_before_finalize": True,
                "can_skip_with_reason": True,
            },
        }

    def recap_submit(
        self,
        recap_id: str,
        answers: Dict,
        confirm_level: str = "draft",
        source_agent: str = "",
    ) -> Dict:
        """Submit three-question answers and generate a structured draft."""
        from core.app.retrospective_session_manager import RetrospectiveSessionManager

        manager = RetrospectiveSessionManager()
        session = manager.get_session(recap_id=recap_id)
        if session and source_agent and session.owner_agent and session.owner_agent != source_agent:
            return {
                "success": False,
                "error": "owner_conflict",
                "recap_id": recap_id,
                "owner_agent": session.owner_agent,
                "source_agent": source_agent,
                "message": "当前 Agent 不是该复盘 owner，不能提交答案。",
            }
        try:
            session = manager.submit_answers(recap_id, {str(k): str(v) for k, v in answers.items()})
        except ValueError as e:
            return {
                "success": False,
                "error": "invalid_state",
                "message": str(e),
                "recap_id": recap_id,
            }
        if confirm_level == "user_confirmed" and session.draft and not session.draft.missing_fields:
            session = manager.confirm(recap_id)
        draft = session.draft.to_dict() if session.draft else None
        return {
            "success": True,
            "recap_id": recap_id,
            "state": session.state,
            "draft": draft,
            "missing_fields": session.draft.missing_fields if session.draft else ["draft"],
            "source_agent": source_agent,
        }

    @staticmethod
    def _recap_owner_denial(
        session: Any,
        source_agent: str,
        action: str,
    ) -> Dict | None:
        """Deny recap object access when the server principal is not owner."""
        if source_agent and session and session.owner_agent and session.owner_agent != source_agent:
            return {
                "success": False,
                "error": "owner_conflict",
                "recap_id": session.recap_id,
                "owner_agent": session.owner_agent,
                "source_agent": source_agent,
                "message": f"当前 Agent 不是该复盘 owner，不能执行 {action}。",
            }
        return None

    def recap_finalize(
        self,
        recap_id: str,
        write_policy: str = "save_and_index",
        follow_up_at: str = "",
        confirmed_by_user: bool = True,
        source_agent: str = "",
    ) -> Dict:
        """Finalize a confirmed recap into the Wiki and route consumption."""
        from core.app.retrospective_consumption_router import RetrospectiveConsumptionRouter
        from core.app.retrospective_models import RetrospectiveRecord
        from core.app.retrospective_session_manager import RetrospectiveSessionManager
        from core.app.retrospective_store import RetrospectiveStore

        manager = RetrospectiveSessionManager()
        session = manager.get_session(recap_id=recap_id)
        if not session:
            return {
                "success": False,
                "error": "not_found",
                "message": f"recap session not found: {recap_id}",
            }
        owner_denial = self._recap_owner_denial(
            session,
            source_agent,
            "finalize",
        )
        if owner_denial:
            return owner_denial
        if session.state in {"finalized", "consumed"}:
            from core.app.retrospective_completion import reopen_missing_terminal_page
            from core.app.recap_consumption import RecapConsumptionLedger
            from core.config import get_config

            action_items = len(session.draft.action_items) if session.draft else 0
            missing_page = reopen_missing_terminal_page(manager, session, get_config().wiki_dir)
            if missing_page:
                return missing_page
            consumption_plan = RecapConsumptionLedger(
                get_config().database_dir / "recap_tasks.db",
                initialize=False,
            ).latest_plan_for_recap(recap_id)
            return {
                "success": True,
                "already_finalized": True,
                "recap_id": recap_id,
                "page_path": session.finalized_page,
                "state": session.state,
                "action_items": action_items,
                "indexed": True,
                "follow_up_scheduled": False,
                "consumption_plan": consumption_plan,
                "terminal": True,
            }
        validation = manager.validate_ready_to_finalize(recap_id)
        if not validation["can_finalize"] and session.state not in {
            "proposal_pending",
            "consumption_pending",
            "retryable_failed",
        }:
            return {
                "success": False,
                "error": "contract_violation",
                "missing_fields": validation["missing_fields"],
                "message": validation["message"],
            }
        if not session.draft:
            return {
                "success": False,
                "error": "contract_violation",
                "missing_fields": ["draft"],
                "message": "复盘未完成：缺少结构化草稿，不能 finalize。",
            }
        pipeline_states = {"proposal_pending", "consumption_pending", "retryable_failed"}
        if session.state in pipeline_states:
            pass
        elif confirmed_by_user:
            session = manager.confirm(recap_id)
        else:
            return {
                "success": False,
                "error": "user_confirmation_required",
                "message": "用户未确认前不能 finalize。",
            }
        draft = session.draft
        if not draft:
            return {
                "success": False,
                "error": "contract_violation",
                "missing_fields": ["draft"],
                "message": "复盘未完成：缺少结构化草稿，不能 finalize。",
            }
        task = manager.forced.get_recap_task(session.task_id)
        record = RetrospectiveRecord(
            draft=draft,
            source=task.source if task else "system",
            source_agent=session.source_agent,
            owner_agent=session.owner_agent,
            source_agents=session.source_agents,
            session_id=session.session_id,
            project=session.project,
            task_type=session.task_type,
            subtype=session.subtype,
            severity=task.severity if task else "medium",
            write_policy="recap_finalize" if write_policy == "save_and_index" else write_policy,
            trigger_reason=self._infer_trigger_reason(task.topic if task else session.topic),
            follow_up_at=follow_up_at,
        )
        store = RetrospectiveStore()
        persisted: Dict[str, Any]
        if session.state == "proposal_pending":
            persisted = dict(session.completion_receipt)
            proposal_id = str(persisted.get("proposal_id") or "")
            page_path = str(persisted.get("page_path") or session.finalized_page)
            proposal_status = "missing"
            try:
                from core.trust.config import load_trusted_push_config
                from core.trust.proposal_queue import ProposalQueue

                trusted_config = load_trusted_push_config(wiki_base=store.wiki_base)
                proposal_status = (
                    ProposalQueue(
                        trusted_config.db_path,
                        wiki_base=store.wiki_base,
                        config=trusted_config,
                    )
                    .get(proposal_id)
                    .status
                )
            except (KeyError, OSError, sqlite3.Error, ValueError):
                proposal_status = "missing"
            page_committed = bool(page_path and (store.wiki_base / page_path).exists())
            if proposal_status in {"rejected", "failed", "missing"} or (
                proposal_status == "committed" and not page_committed
            ):
                reason = (
                    "trusted_proposal_committed_without_page"
                    if proposal_status == "committed"
                    else f"trusted_proposal_{proposal_status}"
                )
                failed_receipt = {
                    **persisted,
                    "status": "retryable_failed",
                    "terminal": False,
                    "terminal_reason": reason,
                    "retry_stage": "proposal",
                }
                manager.mark_pipeline_state(
                    recap_id,
                    "retryable_failed",
                    page_path=page_path,
                    completion_receipt=failed_receipt,
                )
                return {
                    "success": False,
                    "status": "retryable_failed",
                    "state": "retryable_failed",
                    "terminal": False,
                    "page_path": page_path,
                    "indexed": False,
                    "trusted_push": persisted.get("trusted_push", {}),
                    "error": failed_receipt["terminal_reason"],
                }
            if proposal_status != "committed" or not page_committed:
                return {
                    "success": True,
                    "status": "proposal_pending",
                    "state": "proposal_pending",
                    "terminal": False,
                    "page_path": page_path,
                    "indexed": False,
                    "trusted_push": persisted.get("trusted_push", {}),
                    "consumption_plan": None,
                    "required_consumer_receipts": [f"proposal:{proposal_id}", f"page:{page_path}"],
                }
            persisted = {
                **persisted,
                "status": "committed",
                "terminal": True,
                "proposal_status": proposal_status,
            }
            store.mark_recap_confirmed(draft.task_id)
        elif session.state == "consumption_pending" and session.finalized_page:
            page_path = session.finalized_page
            if not (store.wiki_base / page_path).exists():
                return {
                    "success": False,
                    "status": "retryable_failed",
                    "state": session.state,
                    "terminal": False,
                    "page_path": page_path,
                    "indexed": False,
                    "error": "committed retrospective page is missing",
                }
            persisted = dict(session.completion_receipt)
            persisted.update({"status": "committed", "terminal": True, "page_path": page_path})
        else:
            from core.app.retrospective_store import (
                authorize_confirmed_retrospective_write,
            )
            from core.trust.config import load_trusted_push_config

            write_plan = store.prepare_write(record)
            trusted_config = load_trusted_push_config(wiki_base=store.wiki_base)
            material_action = authorize_confirmed_retrospective_write(
                record,
                write_plan,
                state_db_path=(trusted_config.db_path.parent / "producer_consumer_ledger.db"),
                confirmed_by_user=confirmed_by_user,
                source_agent=source_agent,
            )
            persisted = store.save_with_receipt(
                record,
                material_action=material_action,
                plan=write_plan,
            )
            page_path = str(persisted["page_path"])
            if persisted["status"] == "proposal_pending":
                manager.mark_pipeline_state(
                    recap_id,
                    "proposal_pending",
                    page_path=page_path,
                    completion_receipt=persisted,
                )
                return {
                    "success": True,
                    "status": "proposal_pending",
                    "state": "proposal_pending",
                    "terminal": False,
                    "page_path": page_path,
                    "action_items": len(draft.action_items),
                    "indexed": False,
                    "trusted_push": persisted.get("trusted_push", {}),
                    "follow_up_scheduled": False,
                    "consumption_plan": None,
                    "required_consumer_receipts": [
                        f"proposal:{persisted.get('proposal_id', '')}",
                        f"page:{page_path}",
                    ],
                }

        manager.mark_pipeline_state(
            recap_id,
            "consumption_pending",
            page_path=page_path,
            completion_receipt=persisted,
        )
        try:
            plan = RetrospectiveConsumptionRouter().route_after_finalize(
                record,
                page_path=page_path,
            )
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
            return {
                "success": False,
                "status": "retryable_failed",
                "state": "consumption_pending",
                "terminal": False,
                "page_path": page_path,
                "indexed": True,
                "error": str(exc),
            }
        if plan.plan_status != "consumed":
            return {
                "success": False,
                "status": plan.plan_status,
                "state": "consumption_pending",
                "terminal": False,
                "page_path": page_path,
                "indexed": True,
                "error": "required retrospective consumer receipts are incomplete",
                "consumption_plan": plan.to_dict(),
                "failed_targets": plan.failed_targets,
            }
        manager.mark_pipeline_state(
            recap_id,
            "consumption_pending",
            page_path=page_path,
            completion_receipt={
                **persisted,
                "consumption_plan_id": plan.plan_id,
                "consumption_plan_status": plan.plan_status,
                "required_receipt_count": plan.required_receipt_count,
                "terminal_receipt_count": plan.terminal_receipt_count,
            },
        )
        manager.mark_finalized(recap_id, page_path)
        manager.mark_consumed(recap_id)
        trusted_push = persisted.get("trusted_push", {})
        consumer_receipts = [
            f"{item['canonical_target']}:{item['status']}"
            for item in plan.target_statuses
            if item["required"]
        ]
        return {
            "success": True,
            "status": "committed",
            "state": "consumed",
            "terminal": True,
            "page_path": page_path,
            "action_items": len(draft.action_items),
            "indexed": True,
            "trusted_push": trusted_push,
            "follow_up_scheduled": any(
                item["canonical_target"] == "follow_up" and item["status"] == "committed"
                for item in plan.target_statuses
            ),
            "consumption_plan": plan.to_dict(),
            "required_consumer_receipts": consumer_receipts,
        }

    def recap_skip(
        self,
        recap_id: str = "",
        task_id: str = "",
        skip_reason: str = "",
        user_note: str = "",
        owner_agent: str = "",
        source_agent: str = "",
    ) -> Dict:
        """Record a structured recap skip event."""
        from core.app.retrospective_session_manager import RetrospectiveSessionManager
        from core.app.retrospective_skip_event_store import RetrospectiveSkipEventStore

        manager = RetrospectiveSessionManager()
        session = manager.get_session(recap_id=recap_id) if recap_id else None
        if not session and task_id:
            session = manager.start(
                task_id=task_id,
                owner_agent=owner_agent or source_agent,
                source_agent=source_agent,
            )
        if not session:
            return {
                "success": False,
                "error": "not_found",
                "message": "recap_id or task_id is required",
            }
        requested_owner = owner_agent or source_agent
        if requested_owner and session.owner_agent and session.owner_agent != requested_owner:
            return {
                "success": False,
                "error": "owner_conflict",
                "recap_id": session.recap_id,
                "owner_agent": session.owner_agent,
                "source_agent": source_agent,
                "message": "当前 Agent 不是该复盘 owner，不能记录 skip。",
            }
        if session.state in {"user_confirmed", "finalized", "consumed"}:
            return {
                "success": False,
                "error": "recap_already_closed",
                "recap_id": session.recap_id,
                "state": session.state,
                "message": "复盘已确认或关闭，不能再记录 skip。",
            }
        task = manager.forced.get_recap_task(session.task_id)
        event = RetrospectiveSkipEventStore().record_skip(
            recap_id=session.recap_id,
            task_id=session.task_id,
            skip_reason=skip_reason,
            owner_agent=owner_agent or session.owner_agent,
            user_note=user_note,
            source_agent=source_agent or session.source_agent,
            source_agents=session.source_agents,
            project=session.project,
            task_type=session.task_type,
            trigger_reason=self._infer_trigger_reason(task.topic if task else session.topic),
        )
        skip_complete = event.consumption_plan.get("plan_status") == "consumed"
        response = {
            "success": skip_complete,
            "event_id": event.event_id,
            "recap_id": event.recap_id,
            "task_id": event.task_id,
            "skip_status": event.skip_status,
            "next_policy": event.next_policy,
            "write_to_wiki": event.write_to_wiki,
            "defer_until": event.defer_until,
            "consumption_targets": event.consumption_targets,
            "consumption_plan": event.consumption_plan,
            "terminal": skip_complete,
            "status": event.consumption_plan.get("plan_status", "pending"),
        }
        if not skip_complete:
            response["error"] = "required recap skip consumer receipts are incomplete"
        return response

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
        """Record correction or usefulness feedback for a recap."""
        valid = {"accurate", "inaccurate", "useful", "irrelevant", "outdated"}
        if feedback_type not in valid:
            return {
                "success": False,
                "error": f"feedback_type must be one of {sorted(valid)}",
            }
        from core.app.retrospective_session_manager import RetrospectiveSessionManager

        session = RetrospectiveSessionManager().get_session(recap_id=recap_id)
        if not session:
            return {
                "success": False,
                "error": "not_found",
                "message": "recap session not found",
            }
        owner_denial = self._recap_owner_denial(
            session,
            source_agent,
            "feedback",
        )
        if owner_denial:
            return owner_denial
        try:
            from core.application.recap_feedback_service import (
                route_authenticated_recap_feedback,
            )

            return route_authenticated_recap_feedback(
                session=session,
                feedback_type=feedback_type,
                comment=comment,
                source_agent=source_agent,
                supersedes_event_id=supersedes_event_id,
                principal=principal,
                narrowing=narrowing,
            )
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ) as exc:
            self._logger.warning("recap feedback correction failed: %s", exc, exc_info=True)
            return {
                "success": False,
                "recap_id": recap_id,
                "feedback_type": feedback_type,
                "terminal": False,
                "error": str(exc),
            }

    def recap_status(
        self,
        recap_id: str = "",
        task_id: str = "",
        source_agent: str = "",
    ) -> Dict:
        """Return recap state for ownership and progress checks."""
        from core.app.retrospective_session_manager import RetrospectiveSessionManager

        session = RetrospectiveSessionManager().get_session(recap_id=recap_id, task_id=task_id)
        if not session:
            return {
                "success": False,
                "error": "not_found",
                "message": "recap session not found",
            }
        owner_denial = self._recap_owner_denial(
            session,
            source_agent,
            "status",
        )
        if owner_denial:
            return owner_denial
        validation = RetrospectiveSessionManager().validate_ready_to_finalize(session.recap_id)
        from core.app.recap_consumption import RecapConsumptionLedger
        from core.app.recap_feedback import RecapFeedbackOutbox
        from core.config import get_config

        recap_db = get_config().database_dir / "recap_tasks.db"
        consumption_plan = RecapConsumptionLedger(recap_db, initialize=False).latest_plan_for_recap(
            session.recap_id
        )
        latest_feedback = RecapFeedbackOutbox(recap_db, initialize=False).latest_for_recap(
            session.recap_id
        )
        return {
            "success": True,
            "recap_id": session.recap_id,
            "task_id": session.task_id,
            "state": session.state,
            "owner_agent": session.owner_agent,
            "source_agents": session.source_agents,
            "answered_questions": session.answered_questions(),
            "next_question": session.next_question(),
            "can_finalize": validation["can_finalize"],
            "missing_fields": validation["missing_fields"],
            "finalized_page": session.finalized_page,
            "consumption_plan": consumption_plan,
            "latest_feedback": latest_feedback,
        }

    def recap_claim_owner(
        self,
        recap_id: str,
        owner_agent: str,
        current_session_id: str = "",
    ) -> Dict:
        """Claim the user-interaction owner lock for a recap."""
        from core.app.retrospective_session_manager import RetrospectiveSessionManager

        manager = RetrospectiveSessionManager()
        claimed = manager.claim_owner(recap_id, owner_agent, current_session_id)
        session = manager.get_session(recap_id=recap_id)
        return {
            "success": claimed,
            "recap_id": recap_id,
            "owner_agent": session.owner_agent if session else "",
            "state": session.state if session else "",
        }

    @staticmethod
    def _infer_trigger_reason(topic: str) -> list:
        reasons = []
        if "skipped_low_quality" in topic:
            reasons.append("skipped_low_quality")
        if "skipped_by_pipeline" in topic or "API" in topic:
            reasons.append("skipped_by_pipeline")
        if "重复" in topic or "同类" in topic:
            reasons.append("same_type_repeated")
        return reasons

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
        """Run KIA in-process guard checks."""
        from core.kia.aegis import InProcessGuard
        from core.kia.prophasis import ChecklistItem, LoadedKnowledge

        cache_key = f"{task_type or 'general'}:{subtype or ''}"
        if principal is not None:
            cache_key = f"mcp:{principal.agent}:{cache_key}"
        self._prune_guard_knowledge_cache()
        cached = None if principal is not None else self._guard_knowledge_cache.get(cache_key)
        if cached:
            knowledge, loaded_at = cached
            if datetime.now() - loaded_at < timedelta(seconds=self._guard_cache_ttl_seconds):
                self._logger.debug("[guard_check] 命中知识缓存: %s", cache_key)
            else:
                knowledge = None
        else:
            knowledge = None

        if knowledge is None and principal is None:
            from core.kia.kairos import TimeWindow, TimeWindowType
            from core.kia.prophasis import PreFlightInjector

            try:
                injector = PreFlightInjector()
                time_window = TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0)
                knowledge = injector.inject(task_type, subtype, time_window, "")
            except (
                OSError,
                TypeError,
                ValueError,
                RuntimeError,
                sqlite3.Error,
            ) as exc:
                self._logger.warning("guard preflight degraded: %s", exc)
                knowledge = None
            self._guard_knowledge_cache[cache_key] = (knowledge, datetime.now())
            self._prune_guard_knowledge_cache()

        if not knowledge or not knowledge.checklist:
            knowledge = LoadedKnowledge(
                task_type=task_type or "general",
                subtype=subtype or "",
                version=1,
                checklist=[
                    ChecklistItem(
                        item="涉及删除/覆盖/生产环境/密钥/不可逆迁移的操作，请二次确认",
                        source="default_guard",
                        severity="critical",
                        trigger_keywords=[
                            "删除",
                            "清空",
                            "覆盖",
                            "drop",
                            "truncate",
                            "rm",
                            "生产",
                            "prod",
                            "密钥",
                            "key",
                            "迁移",
                            "migrate",
                        ],
                        risk_patterns=[
                            r"删除(?!\s*注释|\s*行|\s*掉|\s*去).*?(文件|数据|表|目录|数据库|仓库|项目)",
                            r"清空.*?(文件|数据|表|目录|数据库|缓存)",
                            r"覆盖.*?(文件|数据|配置|代码)",
                            r"生产.*?部署|部署.*?生产|线上.*?部署",
                            r"(api[_-]?key|token|密码|secret|密钥|私钥|证书)",
                            r"rm\s+-rf|rm\s+.*?/(重要|生产|线上|正式)",
                            r"drop\s+(table|database|schema)",
                            r"truncate\s+table",
                            r"修改.*?(生产|线上|正式).*?(配置|数据|代码)",
                            r"重启.*?(生产|线上|正式).*?(服务|机器|实例)",
                            r"grant\s+all|revoke\s+|chmod\s+777",
                        ],
                    ),
                    ChecklistItem(
                        item="未测试的代码提交可能导致回滚风险",
                        source="default_guard",
                        severity="high",
                        trigger_keywords=["提交", "commit", "push", "合并", "merge"],
                        risk_patterns=[r"提交.*?(未测试|没测试|无测试)"],
                    ),
                ],
                lessons_summary="",
                loaded_at=datetime.now().isoformat(),
            )
            self._guard_knowledge_cache[cache_key] = (knowledge, datetime.now())
            self._prune_guard_knowledge_cache()

        policy_patches = self._active_policy_patches(
            task_type or "general",
            subtype,
            " ".join([user_message or "", ai_response or "", str(context or {})]),
            scope=(narrowing.project if narrowing and narrowing.project else "global"),
        )
        policy_patch_items = self._policy_patch_checklist_items(policy_patches)
        policy_patch_dicts = self._policy_patch_dicts(policy_patches)
        if policy_patch_items:
            knowledge = LoadedKnowledge(
                task_type=knowledge.task_type,
                subtype=knowledge.subtype,
                version=knowledge.version,
                checklist=list(knowledge.checklist) + policy_patch_items,
                lessons_summary=knowledge.lessons_summary,
                loaded_at=knowledge.loaded_at,
                is_compact=knowledge.is_compact,
                total_items=knowledge.total_items + len(policy_patch_items),
                hit_items=knowledge.hit_items,
                ignored_items=knowledge.ignored_items,
            )

        sig_hasher = hashlib.md5(usedforsecurity=False)
        for item in knowledge.checklist:
            sig_hasher.update(item.item.encode("utf-8"))
            sig_hasher.update(b"\n")
        checklist_hash = sig_hasher.hexdigest()[:16]
        guard_key = f"{task_type or 'general'}:{subtype or ''}:{checklist_hash}"
        self._prune_guard_sessions()
        guard_entry = self._guard_sessions.get(guard_key)
        if guard_entry is None:
            guard = InProcessGuard(knowledge)
            self._guard_sessions[guard_key] = (guard, datetime.now())
            self._prune_guard_sessions()
        else:
            guard, _ = guard_entry
        alert = guard.check(user_message, ai_response, context=context)

        if not alert:
            return {
                "success": True,
                "alert": False,
                "message": "无风险触发",
                "policy_patches": policy_patch_dicts,
            }

        level_val = alert.level.value
        risk_level = (
            "alert" if level_val == "interrupt" else "warn" if level_val == "hint" else "info"
        )
        response = {
            "success": True,
            "alert": True,
            "risk_level": risk_level,
            "level": level_val,
            "triggered_by": alert.triggered_by,
            "trigger_text": alert.trigger_text,
            "suggestion": alert.suggestion,
            "checklist_item": alert.checklist_item.item if alert.checklist_item else "",
            "severity": alert.checklist_item.severity if alert.checklist_item else "medium",
            "policy_patches": policy_patch_dicts,
        }
        if getattr(alert, "metadata", None):
            response["metadata"] = dict(alert.metadata)
            for key in ("threshold_source", "threshold_value", "current_count"):
                if key in alert.metadata:
                    response[key] = alert.metadata[key]
        delivery_level = (
            "force_open" if risk_level == "alert" else "warn" if risk_level == "warn" else "hint"
        )
        delivery_subject = str(response["checklist_item"] or alert.triggered_by or user_message)
        response.update(
            self._route_delivery_event(
                source="guard_check",
                subject=delivery_subject,
                channel="guard_check",
                target=str(alert.triggered_by or ""),
                evidence_refs=[alert.checklist_item.source] if alert.checklist_item else [],
                task_fit_score=0.9 if risk_level in {"alert", "warn"} else 0.75,
                requested_level=delivery_level,
                task_key=task_type or "guard_check",
                cooldown_key=delivery_subject,
                active_risk=risk_level in {"alert", "warn"},
                metadata={
                    "task_type": task_type,
                    "subtype": subtype,
                    "trigger_text": alert.trigger_text,
                    "severity": response["severity"],
                    "level": level_val,
                    "context_keys": sorted((context or {}).keys()),
                },
            )
        )
        return response
