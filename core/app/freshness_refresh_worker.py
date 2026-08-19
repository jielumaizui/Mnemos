# -*- coding: utf-8 -*-
"""
FreshnessRefreshWorker — 知识新鲜度自动刷新

把 freshness_check 从"只报警"推进到"可手动/自动刷新"：
- 对单个页面重新蒸馏并更新 frontmatter 日期
- 批量扫描 stale/version-bound 页面并刷新
- 刷新前自动备份到 07-Shadow/08-Refresh/
- timeless 页面强制跳过
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionCoordinator,
    MaterialActionRequest,
    authorize_exact_project_contract_action,
    find_pending_material_action_authorization,
    resolve_material_action_authorization,
)
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.config import get_config
from core.frontmatter import fm_get, parse_frontmatter, write_frontmatter
from core.kia.proteus import KnowledgeFreshnessChecker
from core.trust.markdown_adapter import read_markdown_text
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
    TrustedVaultMutationService,
    commit_trusted_markdown,
    commit_trusted_markdown_move,
    trusted_markdown_material_action_binding,
)
from core.trust.models import sha256_text
from core.telemetry.prompt_call_log import (
    ModelCallLedger,
    ModelCallReservation,
    metered_provider_usage,
)
from core.telemetry.provider_request import (
    canonical_chat_input,
    non_redirecting_openai_client,
    safe_provider_error_category,
    utf8_token_upper_bound,
)

EXCLUDED_DIR_NAMES = {"99-Archive", "99-Reports", "07-Shadow", "00-Inbox"}
FRESHNESS_REDISTILL_MAX_TOKENS = 6000
FRESHNESS_DECISION_CONTRACT_ID = "project-contract:freshness-material-actions"
FRESHNESS_DECISION_CONTRACT_REVISION = "mnemos.freshness_material_actions.v1"
FRESHNESS_DECISION_CONTRACT_TEXT = (
    "The freshness worker may refresh, back up, or archive only an exact page "
    "that its current temporal policy classified as eligible."
)
FRESHNESS_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.app.freshness_refresh_worker",
        "producer": "FreshnessRefreshWorker",
        "version": FRESHNESS_DECISION_CONTRACT_REVISION,
    }
)

logger = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    """单次刷新结果"""

    status: str  # refreshed / skipped / error
    path: str
    reason: str = ""
    backup_path: str = ""
    updated_at: str = ""
    error: str = ""


class FreshnessRefreshWorker:
    """知识页面刷新 worker"""

    def __init__(
        self,
        wiki_base: Optional[str] = None,
        backup_dir: Optional[str] = None,
        redistill_enabled: Optional[bool] = None,
        *,
        material_action_resolver: Callable[
            [Mapping[str, str]], MaterialActionAuthorization
        ]
        | None = None,
    ):
        cfg = get_config()
        self._config = cfg
        self._material_action_resolver = material_action_resolver
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else cfg.wiki_dir
        self.backup_dir = (
            Path(backup_dir).expanduser()
            if backup_dir
            else self.wiki_base / "07-Shadow" / "08-Refresh"
        )
        if redistill_enabled is None:
            redistill_enabled = bool(cfg.get("freshness_refresh.redistill_enabled", False))
        self.redistill_enabled = redistill_enabled

    def _resolve_path(self, page_path: str) -> Path:
        p = Path(page_path).expanduser()
        if p.is_absolute():
            return p
        return self.wiki_base / page_path

    @staticmethod
    def _model_call_subject_scope(fm: Dict, page_path: Path) -> tuple[str, str]:
        """Bind refresh traffic to the source asset instead of prompt content."""
        for key in ("session_id", "session"):
            value = str(fm_get(fm, key) or "").strip()
            if value:
                return "session", value
        project = str(fm_get(fm, "project") or "").strip()
        if project:
            return "project", project
        for key in ("raw_event_id", "source_event_id"):
            value = str(fm_get(fm, key) or "").strip()
            if value:
                return "raw_event_id", value
        return "path", str(page_path.expanduser().resolve(strict=False))

    def _backup_path(self, page_path: Path) -> Path:
        """Resolve the shadow backup object without mutating the filesystem."""

        rel = page_path.relative_to(self.wiki_base)
        return self.backup_dir / rel

    def _resolve_material_action(
        self,
        binding: Mapping[str, str],
        command_ids: Mapping[str, str] | None,
        *,
        source_facts: Mapping[str, Any],
        task: str,
        goal: str,
        approved_candidate_key: str,
        approved_candidate_summary: str,
        rejected_candidate_key: str,
        rejected_candidate_summary: str,
        committed_metric: str,
        rejected_metric: str,
    ) -> MaterialActionAuthorization:
        service = TrustedVaultMutationService(wiki_base=self.wiki_base)
        expected_state_db = (
            service.config.db_path.parent / "producer_consumer_ledger.db"
        )
        request = {**dict(binding), "expected_state_db": str(expected_state_db)}
        if self._material_action_resolver is not None:
            return self._material_action_resolver(request)
        if isinstance(command_ids, Mapping):
            command_id = str(
                command_ids.get(binding["target_ref"]) or ""
            ).strip()
            if not command_id:
                raise PermissionError(
                    "freshness mutation lacks its exact material command"
                )
            return MaterialActionCoordinator(
                CognitiveStateStore(expected_state_db)
            ).bind(
                command_id,
                executor_id=TRUSTED_MARKDOWN_EXECUTOR,
            )
        try:
            authorization, _ = resolve_material_action_authorization(
                None,
                owner=TRUSTED_MARKDOWN_OWNER,
                executor_id=TRUSTED_MARKDOWN_EXECUTOR,
                action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
                target_ref=str(binding["target_ref"]),
                input_hash=str(binding["input_hash"]),
                expected_state_db=expected_state_db,
            )
            return authorization
        except PermissionError as exc:
            if "canonical material-action authorization is required" not in str(exc):
                raise
        material_request = MaterialActionRequest(
            owner=TRUSTED_MARKDOWN_OWNER,
            executor_id=TRUSTED_MARKDOWN_EXECUTOR,
            action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
            target_ref=str(binding["target_ref"]),
            input_hash=str(binding["input_hash"]),
            expected_state_db=str(expected_state_db),
        )
        pending = find_pending_material_action_authorization(
            state_db_path=expected_state_db,
            owner=material_request.owner,
            executor_id=material_request.executor_id,
            action_type=material_request.action_type,
            target_ref=material_request.target_ref,
            input_hash=material_request.input_hash,
        )
        if pending is not None:
            return pending
        decision_created_at = datetime.now().astimezone().isoformat()
        return authorize_exact_project_contract_action(
            expected_request=material_request,
            state_db_path=expected_state_db,
            contract_id=FRESHNESS_DECISION_CONTRACT_ID,
            contract_revision_id=FRESHNESS_DECISION_CONTRACT_REVISION,
            contract_text=FRESHNESS_DECISION_CONTRACT_TEXT,
            source_namespace="freshness-material-action",
            source_facts={
                **dict(source_facts),
                "decision_created_at": decision_created_at,
            },
            decision_checks={
                "registered_freshness_operation": str(
                    source_facts.get("operation") or ""
                )
                in {
                    "refresh_page",
                    "backup_refresh_source",
                    "archive_cold_page",
                },
                "material_binding_complete": bool(
                    binding.get("target_ref") and binding.get("input_hash")
                ),
                "source_facts_present": bool(source_facts),
            },
            evidence_refs=(
                f"freshness-target:{binding['target_ref']}",
                f"freshness-input:{binding['input_hash']}",
            ),
            task=task,
            goal=goal,
            constraints=(
                "The temporal eligibility and source content hash must remain exact.",
                "The target content and observed before state cannot drift.",
            ),
            created_at=decision_created_at,
            producer="freshness-refresh-worker",
            producer_version=FRESHNESS_DECISION_CONTRACT_REVISION,
            producer_code_hash=FRESHNESS_DECISION_PRODUCER_HASH,
            evaluator_id="freshness-material-action-evaluator",
            approved_candidate_key=approved_candidate_key,
            approved_candidate_summary=approved_candidate_summary,
            rejected_candidate_key=rejected_candidate_key,
            rejected_candidate_summary=rejected_candidate_summary,
            approved_reason_code="freshness_exact_action_verified",
            rejected_reason_code="freshness_exact_action_rejected",
            committed_metric=committed_metric,
            rejected_metric=rejected_metric,
        )

    def _is_refreshable(self, fm: Dict, alert: Optional[Dict]) -> bool:
        """判断页面是否允许刷新：timeless 跳过，其他需要是 stale/version-bound。"""
        temporal_scope = (fm_get(fm, "temporal_scope") or "").strip()
        if temporal_scope in ("timeless", "永久"):
            return False
        if alert:
            return True
        # 显式 version-bound 即使未触发 alert（版本已最新）也允许手动刷新日期
        if temporal_scope in ("version-bound", "版本绑定"):
            return True
        return False

    def _redistill_body(
        self,
        fm: Dict,
        body: str,
        *,
        subject_scope: tuple[str, str] | None = None,
    ) -> str:
        """可选：使用 LLM 对正文做轻量级重新蒸馏。默认关闭，失败时保持原文。"""
        if not self.redistill_enabled:
            return body
        try:
            from core.llm_config import resolve_llm_api_chain

            import openai

            runtime_config = get_config()
            openai_error_type = getattr(openai, "OpenAIError", RuntimeError)
            chain = resolve_llm_api_chain(runtime_config)
            candidates = [c for c in chain.all_configs if c.configured]
            if not candidates:
                return body

            title = fm_get(fm, "name") or fm_get(fm, "title") or "知识页面"
            prompt = (
                "请基于以下 Markdown 页面内容重新整理、精炼，"
                "保持知识结构与关键信息不变，输出优化后的 Markdown 正文。\n\n"
                f"# {title}\n\n{body[:8000]}\n\n"
                '请直接返回 JSON：{"body": "优化后的 Markdown 正文"}'
            )
            resolved_subject_scope = subject_scope or ("source", "freshness_refresh")
            retry_attempt = 0
            for api_cfg in candidates:
                active_cfg = api_cfg.active()
                if not active_cfg.configured:
                    continue
                current_attempt = retry_attempt
                reservation: ModelCallReservation | None = None
                try:
                    client_kwargs = {"api_key": active_cfg.api_key}
                    if active_cfg.base_url:
                        client_kwargs["base_url"] = active_cfg.base_url
                    messages = [
                        {"role": "system", "content": "你是知识整理助手。只返回 JSON。"},
                        {"role": "user", "content": prompt},
                    ]
                    provider_input = canonical_chat_input(messages)
                    ledger = ModelCallLedger.for_config(runtime_config)
                    run_id = ledger.start_run(
                        f"freshness-redistill:{uuid.uuid4().hex}",
                        subject_scope=resolved_subject_scope,
                    )
                    reservation = ledger.reserve(
                        run_id=run_id,
                        operation="freshness_redistill",
                        provider=active_cfg.provider,
                        model=active_cfg.model,
                        input_text=provider_input,
                        input_tokens=utf8_token_upper_bound(provider_input),
                        output_tokens=FRESHNESS_REDISTILL_MAX_TOKENS,
                        cache_status="miss",
                        retry_attempt=current_attempt,
                        subject_scopes=(resolved_subject_scope,),
                    )
                    reservation.mark_dispatched()
                    retry_attempt += 1
                    started = time.perf_counter()
                    with non_redirecting_openai_client(
                        openai.OpenAI, **client_kwargs
                    ) as client:  # type: ignore[arg-type]
                        response = client.chat.completions.create(
                            model=active_cfg.model,
                            messages=messages,
                            max_tokens=FRESHNESS_REDISTILL_MAX_TOKENS,
                            response_format={"type": "json_object"},
                            timeout=60,
                        )
                    usage = getattr(response, "usage", None)
                    request_id = str(getattr(response, "id", "") or "")
                    metered_usage = metered_provider_usage(
                        usage,
                        request_id=request_id,
                        output_required=True,
                    )
                    if metered_usage is None:
                        reservation.preserve_incurred(
                            error_code="freshness_provider_usage_missing"
                        )
                    else:
                        reservation.settle(
                            usage=metered_usage,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                        )
                    data = response.choices[0].message.content or "{}"
                    import json

                    parsed = json.loads(data)
                    api_cfg.report_success(active_cfg)
                    return parsed.get("body") or body
                except (
                    openai_error_type,
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    ImportError,
                    AttributeError,
                    IndexError,
                    RuntimeError,
                ) as exc:
                    if reservation is not None:
                        if reservation.dispatched:
                            reservation.preserve_incurred(error_code="freshness_provider_exception")
                        else:
                            reservation.release(error_code="freshness_pre_dispatch_exception")
                    error_category = safe_provider_error_category(exc)
                    api_cfg.report_failure(active_cfg, error_category)
                    logger.warning(
                        "[freshness_refresh] LLM redistillation failed (%s/%s): category=%s",
                        active_cfg.provider,
                        active_cfg.model,
                        error_category,
                    )
            return body
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ) as exc:
            logger.warning(
                "[freshness_refresh] LLM redistillation failed; preserving source: category=%s",
                safe_provider_error_category(exc),
            )
            return body

    def refresh_page(
        self,
        page_path: str,
        *,
        material_action_commands: Mapping[str, str] | None = None,
    ) -> RefreshResult:
        """刷新单个页面：备份、可选重新蒸馏、更新日期。"""
        path = self._resolve_path(page_path)
        rel = str(path.relative_to(self.wiki_base)) if self.wiki_base in path.parents else page_path

        if not path.exists():
            return RefreshResult(status="error", path=rel, error="页面不存在")

        try:
            content = path.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(content)
            if fm is None:
                fm = {}
        except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
            return RefreshResult(status="error", path=rel, error=f"读取失败: {exc}")

        checker = KnowledgeFreshnessChecker()
        alert = checker.check({"frontmatter": fm, "path": str(path)})

        if not self._is_refreshable(fm, alert):  # type: ignore[arg-type]
            reason = (
                "timeless 页面跳过"
                if (fm_get(fm, "temporal_scope") or "") in ("timeless", "永久")
                else "未过期且非 version-bound"
            )
            return RefreshResult(status="skipped", path=rel, reason=reason)

        try:
            new_body = self._redistill_body(
                fm,
                body,
                subject_scope=self._model_call_subject_scope(fm, path),
            )
            now = datetime.now().strftime("%Y-%m-%d")
            fm["updated_at"] = now
            fm["修改日期"] = now
            new_content = write_frontmatter(fm, new_body)
            refresh_binding = trusted_markdown_material_action_binding(
                target_path=path,
                content=new_content,
                proposed_action="refresh_page",
                expected_existing_hash=sha256_text(content),
            )
            refresh_authorization = self._resolve_material_action(
                refresh_binding,
                material_action_commands,
                source_facts={
                    "schema_version": "mnemos.freshness_refresh_facts.v1",
                    "operation": "refresh_page",
                    "page_path": str(path.resolve(strict=False)),
                    "source_content_hash": sha256_text(content),
                    "target_content_hash": sha256_text(new_content),
                    "temporal_scope": str(fm_get(fm, "temporal_scope") or ""),
                    "alert_type": str(getattr(alert, "type", "") or ""),
                    "alert_severity": str(
                        getattr(alert, "severity", "") or ""
                    ),
                    "updated_at": now,
                },
                task=f"Refresh stale Wiki page {rel}",
                goal="Refresh only the exact page classified as stale or version-bound.",
                approved_candidate_key="refresh_exact_temporally_eligible_page",
                approved_candidate_summary=(
                    "Refresh the exact page selected by the current freshness policy."
                ),
                rejected_candidate_key="retain_ineligible_or_drifted_page",
                rejected_candidate_summary=(
                    "Retain a page that is ineligible or changed after freshness evaluation."
                ),
                committed_metric="freshness_page_refresh_receipt",
                rejected_metric="ineligible_freshness_mutation_count",
            )
            trusted = self._submit_trusted_mutation(
                path,
                new_content,
                source="freshness_refresh",
                proposed_action="refresh_page",
                evidence_refs=[rel],
                metadata={"updated_at": now},
                expected_existing_hash=sha256_text(content),
                material_action=refresh_authorization,
            )
            if trusted.intercepted:
                return RefreshResult(
                    status="proposed",
                    path=rel,
                    updated_at=now,
                    reason=f"trusted_proposal:{trusted.proposal_id}",
                )

            backup_path = self._backup_path(path)
            backup_existing = (
                read_markdown_text(backup_path)
                if backup_path.is_file()
                else ""
            )
            backup_binding = trusted_markdown_material_action_binding(
                target_path=backup_path,
                content=content,
                proposed_action="backup_refresh_source",
                expected_existing_hash=sha256_text(backup_existing),
            )
            backup_authorization = self._resolve_material_action(
                backup_binding,
                material_action_commands,
                source_facts={
                    "schema_version": "mnemos.freshness_backup_facts.v1",
                    "operation": "backup_refresh_source",
                    "source_path": str(path.resolve(strict=False)),
                    "source_content_hash": sha256_text(content),
                    "backup_path": str(backup_path.resolve(strict=False)),
                    "backup_existing_hash": sha256_text(backup_existing),
                    "refresh_target_hash": sha256_text(new_content),
                },
                task=f"Back up Wiki page before refresh {rel}",
                goal="Preserve the exact pre-refresh page at its deterministic shadow path.",
                approved_candidate_key="write_exact_pre_refresh_backup",
                approved_candidate_summary=(
                    "Write the exact source bytes to the deterministic refresh backup."
                ),
                rejected_candidate_key="reject_unbound_refresh_backup",
                rejected_candidate_summary=(
                    "Reject a backup not bound to the exact refresh source and target."
                ),
                committed_metric="freshness_backup_receipt",
                rejected_metric="unbound_freshness_backup_count",
            )
            backup_trusted = self._submit_trusted_mutation(
                backup_path,
                content,
                source="freshness_backup",
                proposed_action="backup_refresh_source",
                evidence_refs=[rel],
                metadata={"backup_of": str(path)},
                expected_existing_hash=sha256_text(backup_existing),
                material_action=backup_authorization,
            )
            if backup_trusted.intercepted:
                return RefreshResult(
                    status="proposed",
                    path=rel,
                    updated_at=now,
                    reason=f"trusted_proposal:{backup_trusted.proposal_id}",
                )
            commit_trusted_markdown(
                backup_trusted,
                target_path=backup_path,
                content=content,
                material_action=backup_authorization,
            )
            commit_trusted_markdown(
                trusted,
                target_path=path,
                content=new_content,
                material_action=refresh_authorization,
            )
            logger.info("[freshness_refresh] 已刷新: %s", rel)
            return RefreshResult(
                status="refreshed",
                path=rel,
                backup_path=str(backup_path.relative_to(self.wiki_base)),
                updated_at=now,
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
            logger.warning("[freshness_refresh] 刷新失败 %s: %s", rel, exc)
            return RefreshResult(status="error", path=rel, error=f"刷新失败: {exc}")

    def refresh_all_stale(
        self,
        limit: int = 10,
        *,
        material_action_commands: Mapping[str, str] | None = None,
    ) -> Dict[str, Any]:
        """扫描全 wiki 的过期页面并批量刷新。"""
        limit = max(1, int(limit))
        checker = KnowledgeFreshnessChecker()
        results: List[RefreshResult] = []
        scanned = 0

        if not self.wiki_base.exists():
            return {"status": "error", "error": "wiki_base not found", "results": results}

        for md_file in sorted(self.wiki_base.rglob("*.md")):
            rel_parts = md_file.relative_to(self.wiki_base).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            if md_file.name.endswith(".shadow.md"):
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(content)
                if fm is None:
                    fm = {}
                alert = checker.check({"frontmatter": fm, "path": str(md_file)})
                if not self._is_refreshable(fm, alert):  # type: ignore[arg-type]
                    continue
                scanned += 1
                result = self.refresh_page(
                    str(md_file),
                    material_action_commands=material_action_commands,
                )
                results.append(result)
                if len(results) >= limit:
                    break
            except (OSError, UnicodeDecodeError, ValueError, TypeError, AttributeError) as exc:
                logger.debug("[freshness_refresh] 扫描失败 %s: %s", md_file, exc)
                continue

        refreshed = [r for r in results if r.status == "refreshed"]
        return {
            "status": "ok",
            "scanned": scanned,
            "refreshed": len(refreshed),
            "skipped": len([r for r in results if r.status == "skipped"]),
            "errors": len([r for r in results if r.status == "error"]),
            "results": results,
        }

    def _page_age_days(self, fm: Dict, page_path: Path) -> int:
        """根据 frontmatter updated_at 或文件修改时间计算页面距今天数。"""
        now = datetime.now()
        for key in ("updated_at", "修改日期", "created_at", "创建日期"):
            val = fm_get(fm, key)
            if val:
                try:
                    dt = datetime.fromisoformat(str(val))
                    return (now - dt).days
                # DEBT(S8): 容错跳过，避免单条记录中断批量处理
                except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                    continue
        try:
            mtime = page_path.stat().st_mtime
            return (now - datetime.fromtimestamp(mtime)).days
        # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        except (
            OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
        ):
            return 0

    def _is_excluded_path(self, rel_parts: tuple) -> bool:
        """排除 Archive/Shadow/Inbox 等特殊目录。"""
        return any(part in EXCLUDED_DIR_NAMES or part.startswith(".") for part in rel_parts)

    def archive_cold_pages(
        self,
        cutoff_days: int | None = None,
        limit: int | None = None,
        *,
        material_action_commands: Mapping[str, str] | None = None,
    ) -> Dict[str, Any]:
        """将长时间未更新的页面归档到 99-Archive/Cold/。

        Args:
            cutoff_days: 超过多少天视为冷知识，默认读取 distill.cold_knowledge_archive_days
            limit: 最大归档数量，None 表示不限制
        """
        cfg = get_config()
        if cutoff_days is None:
            cutoff_days = int(cfg.get("distill.cold_knowledge_archive_days", 90) or 90)
        if cutoff_days <= 0:
            return {"status": "ok", "archived": 0, "skipped": 0, "errors": 0, "results": []}

        archive_root = self.wiki_base / "99-Archive" / "Cold"
        results: List[Dict[str, Any]] = []
        archived = skipped = errors = proposed = 0

        if not self.wiki_base.exists():
            return {"status": "error", "error": "wiki_base not found", "results": results}

        for md_file in sorted(self.wiki_base.rglob("*.md")):
            if limit is not None and archived >= limit:
                break
            rel_parts = md_file.relative_to(self.wiki_base).parts
            if self._is_excluded_path(rel_parts):
                continue
            if md_file.name.endswith(".shadow.md"):
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(content)
                if fm is None:
                    fm = {}
                # 已归档页面跳过
                status = (fm_get(fm, "status") or "").strip().lower()
                if status in ("archived", "deprecated", "deleted"):
                    skipped += 1
                    continue
                age_days = self._page_age_days(fm, md_file)
                if age_days < cutoff_days:
                    continue
                rel = md_file.relative_to(self.wiki_base)
                dest = archive_root / rel
                if dest.exists():
                    skipped += 1
                    continue
                fm["status"] = "archived"
                fm["archived_at"] = datetime.now().strftime("%Y-%m-%d")
                new_content = write_frontmatter(fm, body)
                destination_existing_hash = sha256_text("")
                source_content_hash = sha256_text(content)
                archive_binding = trusted_markdown_material_action_binding(
                    target_path=dest,
                    content=new_content,
                    proposed_action="archive_cold_page",
                    expected_existing_hash=destination_existing_hash,
                    source_path=md_file,
                    source_content_hash=source_content_hash,
                )
                archive_authorization = self._resolve_material_action(
                    archive_binding,
                    material_action_commands,
                    source_facts={
                        "schema_version": "mnemos.freshness_archive_facts.v1",
                        "operation": "archive_cold_page",
                        "source_path": str(md_file.resolve(strict=False)),
                        "source_content_hash": source_content_hash,
                        "archive_path": str(dest.resolve(strict=False)),
                        "archive_content_hash": sha256_text(new_content),
                        "age_days": int(age_days),
                        "cutoff_days": int(cutoff_days),
                        "prior_status": status,
                    },
                    task=f"Archive cold Wiki page {rel}",
                    goal="Move only the exact page that exceeds the configured cold threshold.",
                    approved_candidate_key="archive_exact_policy_eligible_cold_page",
                    approved_candidate_summary=(
                        "Archive the exact page whose measured age exceeds the current cutoff."
                    ),
                    rejected_candidate_key="retain_ineligible_or_drifted_cold_page",
                    rejected_candidate_summary=(
                        "Retain a page that is too recent, excluded, pre-archived, or changed."
                    ),
                    committed_metric="freshness_archive_receipt",
                    rejected_metric="ineligible_cold_page_archive_count",
                )
                trusted = self._submit_trusted_mutation(
                    dest,
                    new_content,
                    source="freshness_archive",
                    proposed_action="archive_cold_page",
                    evidence_refs=[str(rel)],
                    metadata={
                        "source_path": str(md_file),
                        "source_content_hash": source_content_hash,
                        "archive_path": str(dest),
                    },
                    expected_existing_hash=destination_existing_hash,
                    material_action=archive_authorization,
                )
                if trusted.intercepted:
                    proposed += 1
                    results.append(
                        {
                            "path": str(rel),
                            "status": "proposed",
                            "age_days": age_days,
                            "archive_path": str(dest.relative_to(self.wiki_base)),
                            "proposal_id": trusted.proposal_id,
                        }
                    )
                    continue
                commit_trusted_markdown_move(
                    trusted,
                    source_path=md_file,
                    target_path=dest,
                    content=new_content,
                    material_action=archive_authorization,
                )
                archived += 1
                results.append(
                    {
                        "path": str(rel),
                        "status": "archived",
                        "age_days": age_days,
                        "archive_path": str(dest.relative_to(self.wiki_base)),
                    }
                )
                logger.info("[freshness_refresh] 已归档冷知识: %s (age=%d)", rel, age_days)
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
                errors += 1
                logger.warning("[freshness_refresh] 归档失败 %s: %s", md_file, exc)

        return {
            "status": "ok",
            "cutoff_days": cutoff_days,
            "archived": archived,
            "proposed": proposed,
            "skipped": skipped,
            "errors": errors,
            "results": results,
        }

    def _submit_trusted_mutation(
        self,
        path: Path,
        content: str,
        *,
        source: str,
        proposed_action: str,
        evidence_refs: list[str],
        metadata: Dict[str, Any],
        expected_existing_hash: str | None = None,
        material_action: MaterialActionAuthorization | None = None,
    ):
        return TrustedVaultMutationService(wiki_base=self.wiki_base).submit_markdown(
            target_path=path,
            content=content,
            source=source,
            actor="system",
            evidence_refs=evidence_refs,
            proposed_action=proposed_action,
            expected_existing_hash=expected_existing_hash,
            metadata=metadata,
            material_action=material_action,
        )

    def list_pages(self, status_filter: str = "all") -> List[Dict[str, Any]]:
        """列出 wiki 页面的新鲜度状态。"""
        checker = KnowledgeFreshnessChecker()
        pages: List[Dict[str, Any]] = []

        if not self.wiki_base.exists():
            return pages

        for md_file in sorted(self.wiki_base.rglob("*.md")):
            rel_parts = md_file.relative_to(self.wiki_base).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            if md_file.name.endswith(".shadow.md"):
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(content)
                if fm is None:
                    fm = {}
                temporal_scope = (fm_get(fm, "temporal_scope") or "").strip()
                if temporal_scope in ("timeless", "永久"):
                    if status_filter in ("all", "fresh"):
                        pages.append(
                            {
                                "path": str(md_file.relative_to(self.wiki_base)),
                                "status": "fresh",
                                "reason": "timeless",
                            }
                        )
                    continue
                alert = checker.check({"frontmatter": fm, "path": str(md_file)})
                if alert:
                    if status_filter in ("all", "stale"):
                        pages.append(
                            {
                                "path": str(md_file.relative_to(self.wiki_base)),
                                "status": "stale",
                                "type": alert.type,
                                "severity": alert.severity,
                                "message": alert.message,
                            }
                        )
                else:
                    if status_filter in ("all", "fresh"):
                        pages.append(
                            {
                                "path": str(md_file.relative_to(self.wiki_base)),
                                "status": "fresh",
                                "reason": "",
                            }
                        )
            except (OSError, UnicodeDecodeError, ValueError, TypeError, AttributeError) as exc:
                logger.debug("[freshness_refresh] 列出页面失败 %s: %s", md_file, exc)
                continue

        return pages
